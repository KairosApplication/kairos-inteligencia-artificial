import os
import sys
from datetime import datetime, timezone
from typing import Optional, TypedDict, Literal

from bson import ObjectId
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# O console do Windows costuma usar um code page legado (cp1252/cp850)
# que não cobre todos os caracteres Unicode que os modelos podem gerar
# (ex.: hífen não separável, aspas tipográficas). Sem isso, print()
# pode lançar UnicodeEncodeError e derrubar o chat no meio de uma
# resposta.
if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from langchain_groq import ChatGroq
from langchain.agents import create_agent
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from prompts import (GUARDRAIL_PROMPT, ORQUESTRADOR_PROMPT, FAQ_PROMPT, ESTOQUE_PROMPT, VENDAS_PROMPT, CLIENTES_PROMPT, JUIZ_PROMPT, MEMORIA_PROMPT)
from tools.faq import FAQ_TOOLS
from tools.estoque import ESTOQUE_TOOLS
from tools.vendas import VENDAS_TOOLS
from tools.clientes import CLIENTES_TOOLS
from data.memory_store import (
    obter_ou_criar_conversa_ativa,
    carregar_mensagens_contexto,
    contar_mensagens_brutas_carregadas,
    registrar_turno
)
from data.conversation_middleware import MongoSummarizationMiddleware, MemoryContext
from data.observability import (
    iniciar_execucao,
    finalizar_execucao,
    registrar_trace_agente,
    registrar_evento_guardrail,
    registrar_avaliacao_juiz,
    registrar_feedback,
    registrar_memorias,
    carregar_memorias,
    extrair_uso_mensagem,
    extrair_uso_mensagens,
    calcular_custo_usd
)

# CONFIGURAÇÃO
load_dotenv()


def _agora_dt() -> datetime:
    """
    Retorna o instante atual em UTC, utilizado para medir a latência
    de cada nó do grafo antes de registrá-la em `agent_traces`.
    """
    return datetime.now(timezone.utc)

# MEMÓRIA DE LONGO PRAZO
#
# Usuário padrão do chat de teste. Cada user_id possui sua própria
# conversa ativa e memória persistida no MongoDB (coleção
# `conversations` do banco `kairos_ai`).
USUARIO_PADRAO_ID = 1

# Limiares do SummarizationMiddleware. Intencionalmente baixos para
# que a sumarização seja fácil de observar durante os testes; em um
# ambiente real esses valores tendem a ser maiores (ex.: na faixa de
# várias dezenas de mensagens ou milhares de tokens).
RESUMO_GATILHO_MENSAGENS = 10
RESUMO_MANTER_MENSAGENS = 4

# LLMs
llm_groq = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.7,
    api_key=os.getenv("GROQ_API_KEY")
)

llm_rapido = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0.0,
    api_key=os.getenv("GROQ_API_KEY")
)

# SCHEMAS DE SAÍDA ESTRUTURADA
class AvaliacaoGuardrail(BaseModel):
    """
    Define a saída esperada do Guardrail de Segurança.
    """

    bloqueado: bool = Field(description="Indica se a mensagem representa uma tentativa de manipulação e deve ser bloqueada.")

    categoria: Literal[
        "instrucao_maliciosa",
        "extracao_informacao_interna",
        "subversao_papel",
        "injecao_indireta",
        "acao_nao_autorizada",
        "nenhuma"
    ] = Field(description="Categoria de tentativa de manipulação identificada, ou 'nenhuma' se a mensagem for legítima.")

    motivo: str = Field(description="Motivo objetivo da decisão, sem repetir o conteúdo malicioso identificado.")

class Roteamento(BaseModel):
    """
    Define a saída esperada do Orquestrador.
    """

    rota: Literal["faq", "estoque", "vendas", "clientes"] = Field(description="Categoria do especialista que deve atender à solicitação.")

    @field_validator("rota", mode="before")
    @classmethod
    def normalizar_rota(cls, valor):
        """
        Normaliza a rota retornada pelo modelo (remove espaços e
        converte para minúsculas) antes da validação do Literal.

        A API da Groq não garante 100% de aderência ao schema
        (o modo `strict` não é suportado por todos os modelos), então
        o modelo eventualmente retorna a rota em maiúsculas (ex.:
        "FAQ") mesmo com o schema definindo apenas valores em
        minúsculas. Essa normalização evita que isso quebre o parser.
        """
        if isinstance(valor, str):
            return valor.strip().lower()
        return valor

class AvaliacaoJuiz(BaseModel):
    """
    Define a saída esperada do Juiz.
    """

    aprovado: bool = Field(description="Indica se a resposta pode ser entregue ao usuário.")

    motivo: str = Field(description="Motivo objetivo da decisão.")


class MemoriaExtraida(BaseModel):
    """
    Define uma única memória de longo prazo extraída de um turno.
    """

    tipo: Literal["preferencia", "fato", "restricao"] = Field(description="Categoria da memória extraída.")

    conteudo: str = Field(description="Descrição curta e objetiva da memória, independente do restante da conversa.")

    importancia: int = Field(description="Relevância da memória para atendimentos futuros, de 1 a 5.", ge=1, le=5)


class ExtracaoMemoria(BaseModel):
    """
    Define a saída esperada do Agente de Memória de Longo Prazo.
    """

    memorias: list[MemoriaExtraida] = Field(description="Lista de memórias extraídas do turno. Pode ser vazia.")


# GUARDRAIL DE SEGURANÇA
# Executado antes do Orquestrador. Não é um agente com tools.
# Ele apenas avalia a mensagem do usuário e retorna uma decisão
# estruturada sobre bloqueio.
#
# `include_raw=True` faz com que a chamada também devolva a
# AIMessage bruta do modelo (com `usage_metadata`), utilizada em
# `observability.py` para estimar tokens e custo de cada nó do
# grafo, registrados na collection `agent_traces`.
guardrail = llm_rapido.with_structured_output(
    AvaliacaoGuardrail,
    method="json_schema",
    strict=True,
    include_raw=True
)

# ORQUESTRADOR
# O Orquestrador não é um agente com tools.
# Ele apenas classifica a intenção e retorna uma rota estruturada.
orquestrador = llm_rapido.with_structured_output(
    Roteamento,
    method="json_schema",
    strict=True,
    include_raw=True
)

# MIDDLEWARE DE SUMARIZAÇÃO COM MEMÓRIA DE LONGO PRAZO
#
# Cada agente especialista recebe sua própria instância do
# middleware. Ele monitora o histórico de mensagens de cada execução
# e, ao atingir o gatilho configurado em `trigger`, substitui as
# mensagens mais antigas por um resumo gerado pelo `llm_rapido`.
#
# O `MongoSummarizationMiddleware` (ver data/conversation_middleware.py)
# estende esse comportamento padrão do LangChain para também persistir
# o resumo gerado na conversa do usuário no MongoDB, via o
# `MemoryContext` fornecido em `context` no momento do `invoke`.
resumo_middleware = MongoSummarizationMiddleware(
    model=llm_rapido,
    trigger=("messages", RESUMO_GATILHO_MENSAGENS),
    keep=("messages", RESUMO_MANTER_MENSAGENS)
)

# AGENTE FAQ
agente_faq = create_agent(
    model=llm_rapido,
    tools=FAQ_TOOLS,
    system_prompt=FAQ_PROMPT,
    middleware=[resumo_middleware],
    context_schema=MemoryContext
)

# AGENTE ESTOQUE
agente_estoque = create_agent(
    model=llm_groq,
    tools=ESTOQUE_TOOLS,
    system_prompt=ESTOQUE_PROMPT,
    middleware=[resumo_middleware],
    context_schema=MemoryContext
)

# AGENTE VENDAS
agente_vendas = create_agent(
    model=llm_groq,
    tools=VENDAS_TOOLS,
    system_prompt=VENDAS_PROMPT,
    middleware=[resumo_middleware],
    context_schema=MemoryContext
)

# AGENTE CLIENTES
agente_clientes = create_agent(
    model=llm_groq,
    tools=CLIENTES_TOOLS,
    system_prompt=CLIENTES_PROMPT,
    middleware=[resumo_middleware],
    context_schema=MemoryContext
)

# JUIZ
# Assim como o Orquestrador, o Juiz não precisa de tools.
# Ele apenas avalia a resposta e retorna uma decisão estruturada.
juiz = llm_groq.with_structured_output(
    AvaliacaoJuiz,
    method="json_schema",
    strict=True,
    include_raw=True
)

# AGENTE DE MEMÓRIA DE LONGO PRAZO
# Executado depois do Juiz, somente quando a resposta é aprovada.
# Extrai fatos/preferências duradouros sobre o usuário para a
# collection `user_memory`, reaproveitados como contexto adicional
# em turnos futuros.
extrator_memoria = llm_rapido.with_structured_output(
    ExtracaoMemoria,
    method="json_schema",
    strict=True,
    include_raw=True
)

# ESTADO DO GRAFO
class GraphState(TypedDict):
    pergunta: str
    resposta: str
    rota: str
    aprovado: bool
    motivo_avaliacao: str
    tentativas: int
    bloqueado: bool
    categoria_bloqueio: str
    # Memória de longo prazo (preenchidos pelo memoria_node)
    user_id: int
    conversation_id: object
    historico: list[BaseMessage]
    memory_context: MemoryContext
    memorias_usuario: list[dict]
    # Observabilidade (preenchidos pelo memoria_node / consumidos por
    # todos os demais nós para registrar seus próprios traces)
    execution_id: object


# NODE - MEMÓRIA DE LONGO PRAZO
def memoria_node(state: GraphState):
    """
    Carrega, do MongoDB, a conversa ativa do usuário, o histórico de
    contexto (resumo mais recente + janela de mensagens brutas ainda
    não resumidas) e as memórias de longo prazo já extraídas sobre o
    usuário (collection `user_memory`).

    Também abre o registro de observabilidade da execução atual
    (collection `ai_executions`), que será encerrado por
    `guardrail_node` (quando a mensagem for bloqueada) ou por
    `juiz_node` (quando o fluxo terminar em uma resposta entregue).

    Executado antes do Guardrail, no início de cada turno.
    """
    inicio = _agora_dt()

    user_id = state.get("user_id", USUARIO_PADRAO_ID)
    pergunta = state["pergunta"]

    conversa = obter_ou_criar_conversa_ativa(user_id)
    historico = carregar_mensagens_contexto(conversa)
    memorias_usuario = carregar_memorias(user_id)

    tem_resumo = bool(conversa.get("summary"))
    quantidade_brutas = contar_mensagens_brutas_carregadas(conversa)

    memory_context = MemoryContext(
        conversation_id=conversa["_id"],
        offset_summary=1 if tem_resumo else 0,
        offset_raw=quantidade_brutas
    )

    execution_id = iniciar_execucao(user_id, conversa["_id"], pergunta)

    registrar_trace_agente(
        execution_id,
        "memoria",
        "OK",
        inicio,
        _agora_dt(),
        entrada={"user_id": user_id},
        saida={
            "conversation_id": str(conversa["_id"]),
            "mensagens_historico": len(historico),
            "memorias_usuario": len(memorias_usuario)
        }
    )

    return {
        "user_id": user_id,
        "conversation_id": conversa["_id"],
        "historico": historico,
        "memory_context": memory_context,
        "memorias_usuario": memorias_usuario,
        "execution_id": execution_id
    }


def _montar_mensagens_especialista(
    pergunta: str, historico: list[BaseMessage], memorias: list[dict]
) -> list[BaseMessage]:
    """
    Monta a lista de mensagens enviada ao especialista: o histórico
    de contexto (resumo + janela recente), seguido, quando houver
    memórias de longo prazo já extraídas sobre o usuário, de uma
    mensagem adicional com esse contexto, e por fim a pergunta atual.

    Essa mensagem extra é adicionada sempre DEPOIS do histórico
    carregado do MongoDB (nunca misturada a ele), para não afetar a
    contagem de mensagens que o MongoSummarizationMiddleware usa para
    calcular quantas mensagens brutas foram cobertas por um resumo
    (ver `MemoryContext` em data/conversation_middleware.py). Também
    não é persistida na conversa, servindo apenas como contexto deste
    turno.
    """
    mensagens = list(historico)

    if memorias:
        linhas = "\n".join(f"- {memoria['content']}" for memoria in memorias[:5])
        mensagens.append(
            HumanMessage(
                content=(
                    "[Contexto sobre o usuário, não repita este bloco na "
                    f"resposta]\n{linhas}"
                )
            )
        )

    mensagens.append(HumanMessage(content=pergunta))

    return mensagens


# NODE - GUARDRAIL DE SEGURANÇA
def guardrail_node(state: GraphState):
    inicio = _agora_dt()
    pergunta = state["pergunta"]
    execution_id = state["execution_id"]

    saida = guardrail.invoke(
        GUARDRAIL_PROMPT
        + "\n\nMensagem do usuário:\n"
        + pergunta
    )

    resultado: AvaliacaoGuardrail = saida["parsed"]
    tokens_entrada, tokens_saida, _ = extrair_uso_mensagem(saida["raw"])
    custo = calcular_custo_usd(llm_rapido.model_name, tokens_entrada, tokens_saida)

    print(
        "[GUARDRAIL] Resultado: "
        + ("BLOQUEADO" if resultado.bloqueado else "LIBERADO")
    )

    if resultado.bloqueado:
        print(f"[GUARDRAIL] Categoria: {resultado.categoria}")
        print(f"[GUARDRAIL] Motivo: {resultado.motivo}")

    registrar_trace_agente(
        execution_id,
        "guardrail",
        "OK",
        inicio,
        _agora_dt(),
        entrada={"pergunta": pergunta},
        saida={
            "bloqueado": resultado.bloqueado,
            "categoria": resultado.categoria,
            "motivo": resultado.motivo
        },
        model=llm_rapido.model_name,
        estimated_tokens=tokens_entrada + tokens_saida,
        estimated_cost_usd=custo
    )

    registrar_evento_guardrail(
        execution_id, resultado.bloqueado, resultado.categoria, resultado.motivo
    )

    resposta_bloqueio = (
        "Não posso atender a essa solicitação. Se você tiver uma "
        "dúvida sobre o Kairos, sobre estoque, vendas ou clientes, "
        "fico à disposição para ajudar."
    )

    if resultado.bloqueado:
        # A mensagem foi bloqueada: o fluxo termina aqui (ver
        # decidir_bloqueio), então a execução é encerrada agora.
        finalizar_execucao(execution_id, "BLOCKED", resposta_bloqueio)

    return {
        "bloqueado": resultado.bloqueado,
        "categoria_bloqueio": resultado.categoria,
        # Preenche a resposta antecipadamente. Só será usada quando
        # a mensagem for bloqueada (ver decidir_bloqueio).
        "resposta": resposta_bloqueio if resultado.bloqueado else state.get("resposta", "")
    }


# NODE ORQUESTRADOR
def orquestrador_node(state: GraphState):
    inicio = _agora_dt()
    pergunta = state["pergunta"]
    execution_id = state["execution_id"]

    saida = orquestrador.invoke(
        ORQUESTRADOR_PROMPT
        + "\n\nPergunta do usuário:\n"
        + pergunta
    )

    resultado: Roteamento = saida["parsed"]
    tokens_entrada, tokens_saida, _ = extrair_uso_mensagem(saida["raw"])
    custo = calcular_custo_usd(llm_rapido.model_name, tokens_entrada, tokens_saida)

    registrar_trace_agente(
        execution_id,
        "orquestrador",
        "OK",
        inicio,
        _agora_dt(),
        entrada={"pergunta": pergunta},
        saida={"rota": resultado.rota},
        model=llm_rapido.model_name,
        estimated_tokens=tokens_entrada + tokens_saida,
        estimated_cost_usd=custo
    )

    return {"rota": resultado.rota}


def _executar_especialista(
    nome_agente: str, agente, model: str, state: GraphState
) -> dict:
    """
    Lógica comum aos quatro nós especialistas (faq, estoque, vendas,
    clientes): monta as mensagens de entrada (histórico + memórias de
    longo prazo + pergunta atual), invoca o agente, registra o trace
    da execução em `agent_traces` e incrementa o contador de
    tentativas.
    """
    inicio = _agora_dt()
    pergunta = state["pergunta"]
    historico = state.get("historico", [])
    memorias = state.get("memorias_usuario", [])

    mensagens_entrada = _montar_mensagens_especialista(pergunta, historico, memorias)

    resultado = agente.invoke(
        {"messages": mensagens_entrada},
        context=state["memory_context"]
    )

    mensagens_saida = resultado["messages"]
    resposta = mensagens_saida[-1].content

    tokens_entrada, tokens_saida, _ = extrair_uso_mensagens(mensagens_saida)
    custo = calcular_custo_usd(model, tokens_entrada, tokens_saida)

    registrar_trace_agente(
        state["execution_id"],
        nome_agente,
        "OK",
        inicio,
        _agora_dt(),
        entrada={"pergunta": pergunta},
        saida={"resposta": resposta},
        model=model,
        estimated_tokens=tokens_entrada + tokens_saida,
        estimated_cost_usd=custo
    )

    # Incrementa o número de tentativas.
    tentativas = state.get("tentativas", 0) + 1

    return {
        "resposta": resposta,
        "tentativas": tentativas
    }


# NODE FAQ
def faq_node(state: GraphState):
    return _executar_especialista("faq", agente_faq, llm_rapido.model_name, state)


# NODE ESTOQUE
def estoque_node(state: GraphState):
    return _executar_especialista("estoque", agente_estoque, llm_groq.model_name, state)


# NODE VENDAS
def vendas_node(state: GraphState):
    return _executar_especialista("vendas", agente_vendas, llm_groq.model_name, state)


# NODE CLIENTES
def clientes_node(state: GraphState):
    return _executar_especialista("clientes", agente_clientes, llm_groq.model_name, state)


# NODE - JUIZ
def juiz_node(state: GraphState):
    inicio = _agora_dt()
    pergunta = state["pergunta"]
    resposta = state["resposta"]
    execution_id = state["execution_id"]

    prompt_avaliacao = f"""
Pergunta original do usuário: {pergunta}

Resposta produzida pelo agente especialista: {resposta}

Avalie se a resposta produzida pode ser entregue ao usuário.
"""

    saida = juiz.invoke(
        JUIZ_PROMPT
        + "\n\n"
        + prompt_avaliacao
    )

    resultado: AvaliacaoJuiz = saida["parsed"]
    tokens_entrada, tokens_saida, _ = extrair_uso_mensagem(saida["raw"])
    custo = calcular_custo_usd(llm_groq.model_name, tokens_entrada, tokens_saida)

    print("[JUIZ] Resultado: " + ("APROVADO" if resultado.aprovado else "REPROVADO"))

    print(f"[JUIZ] Motivo: {resultado.motivo}")

    registrar_trace_agente(
        execution_id,
        "juiz",
        "OK",
        inicio,
        _agora_dt(),
        entrada={"pergunta": pergunta, "resposta": resposta},
        saida={"aprovado": resultado.aprovado, "motivo": resultado.motivo},
        model=llm_groq.model_name,
        estimated_tokens=tokens_entrada + tokens_saida,
        estimated_cost_usd=custo
    )

    registrar_avaliacao_juiz(execution_id, resultado.aprovado, resultado.motivo)

    # Persiste o turno na memória de longo prazo (MongoDB) somente
    # quando a resposta foi aprovada e será de fato entregue ao
    # usuário. A execução é finalizada aqui, seja a resposta aprovada
    # ou reprovada após esgotar as tentativas (ver decidir_avaliacao).
    tentativas_esgotadas = state.get("tentativas", 0) >= 2

    if resultado.aprovado:
        registrar_turno(state["conversation_id"], pergunta, resposta)
        _extrair_e_registrar_memorias(state["user_id"], pergunta, resposta, execution_id)

    if resultado.aprovado or tentativas_esgotadas:
        status = "SUCCESS" if resultado.aprovado else "REJECTED"
        finalizar_execucao(execution_id, status, resposta)

    return {
        "aprovado": resultado.aprovado,
        "motivo_avaliacao": resultado.motivo
    }


def _extrair_e_registrar_memorias(
    user_id: int, pergunta: str, resposta: str, execution_id: ObjectId
) -> None:
    """
    Executa o Agente de Memória de Longo Prazo sobre o turno recém
    aprovado e persiste, na collection `user_memory`, as memórias
    (preferências, fatos ou restrições) eventualmente extraídas sobre
    o usuário.

    Erros nesta etapa não devem interromper o fluxo principal do
    chat: a memória de longo prazo é um complemento, não um
    pré-requisito para entregar a resposta ao usuário.
    """
    inicio = _agora_dt()

    prompt_extracao = f"""
Pergunta do usuário: {pergunta}

Resposta entregue pelo especialista: {resposta}

Extraia as memórias duradouras sobre o usuário presentes neste turno.
"""

    try:
        saida = extrator_memoria.invoke(
            MEMORIA_PROMPT
            + "\n\n"
            + prompt_extracao
        )

        resultado: ExtracaoMemoria = saida["parsed"]
        tokens_entrada, tokens_saida, _ = extrair_uso_mensagem(saida["raw"])
        custo = calcular_custo_usd(llm_rapido.model_name, tokens_entrada, tokens_saida)

        memorias = [
            {
                "tipo": memoria.tipo,
                "conteudo": memoria.conteudo,
                "importancia": memoria.importancia
            }
            for memoria in resultado.memorias
        ]

        registrar_trace_agente(
            execution_id,
            "memoria_extracao",
            "OK",
            inicio,
            _agora_dt(),
            entrada={"pergunta": pergunta},
            saida={"memorias_extraidas": len(memorias)},
            model=llm_rapido.model_name,
            estimated_tokens=tokens_entrada + tokens_saida,
            estimated_cost_usd=custo
        )

        if memorias:
            registrar_memorias(user_id, memorias)

    except Exception as erro:
        # Falha na extração de memória não deve afetar a resposta já
        # entregue ao usuário; apenas registra o erro no trace.
        registrar_trace_agente(
            execution_id,
            "memoria_extracao",
            "ERROR",
            inicio,
            _agora_dt(),
            entrada={"pergunta": pergunta},
            saida={"erro": str(erro)}
        )


# CONDITIONAL EDGE - GUARDRAIL
def decidir_bloqueio(state: GraphState):
    if state["bloqueado"]:
        return "fim"

    return "orquestrador"


# CONDITIONAL EDGE - ORQUESTRADOR
def decidir_rota(state: GraphState):
    rota = state["rota"]

    if rota == "estoque":
        return "estoque"

    if rota == "vendas":
        return "vendas"

    if rota == "clientes":
        return "clientes"

    # Fallback.
    #
    # Quando os outros especialistas forem implementados,
    # este fallback deverá ser substituído ou tratado
    # explicitamente.
    return "faq"


# CONDITIONAL EDGE - JUIZ
def decidir_avaliacao(state: GraphState):
    # Resposta aprovada
    if state["aprovado"]:
        return "fim"

    # Limite de tentativas
    # Evita que o grafo entre em um loop infinito caso o especialista
    # continue produzindo uma resposta que o Juiz rejeita.
    if state["tentativas"] >= 2:
        return "fim"

    # Resposta reprovada
    # Executa novamente o especialista que gerou a resposta.
    return state["rota"]


# CONSTRUÇÃO DO GRAFO
builder = StateGraph(GraphState)

# NODES
builder.add_node(
    "memoria",
    memoria_node
)

builder.add_node(
    "guardrail",
    guardrail_node
)

builder.add_node(
    "orquestrador",
    orquestrador_node
)

builder.add_node(
    "faq",
    faq_node
)

builder.add_node(
    "estoque",
    estoque_node
)

builder.add_node(
    "vendas",
    vendas_node
)

builder.add_node(
    "clientes",
    clientes_node
)

builder.add_node(
    "juiz",
    juiz_node
)

# EDGES
builder.add_edge(
    START,
    "memoria"
)

builder.add_edge(
    "memoria",
    "guardrail"
)

# GUARDRAIL → ORQUESTRADOR ou FIM (bloqueado)
builder.add_conditional_edges(
    "guardrail",
    decidir_bloqueio,
    {
        "orquestrador": "orquestrador",
        "fim": END
    }
)

# ORQUESTRADOR → ESPECIALISTA
builder.add_conditional_edges(
    "orquestrador",
    decidir_rota,
    {
        "faq": "faq",
        "estoque": "estoque",
        "vendas": "vendas",
        "clientes": "clientes"
    }
)

# FAQ → JUIZ
builder.add_edge(
    "faq",
    "juiz"
)

# ESTOQUE → JUIZ
builder.add_edge(
    "estoque",
    "juiz"
)

# VENDAS → JUIZ
builder.add_edge(
    "vendas",
    "juiz"
)

# CLIENTES → JUIZ
builder.add_edge(
    "clientes",
    "juiz"
)

# JUIZ → FIM ou NOVA TENTATIVA
builder.add_conditional_edges(
    "juiz",
    decidir_avaliacao,
    {
        "fim": END,
        "faq": "faq",
        "estoque": "estoque",
        "vendas": "vendas",
        "clientes": "clientes"
    }
)

# COMPILAÇÃO
grafo = builder.compile()

# FUNÇÃO DE TESTE
def testar_pergunta(pergunta: str, user_id: int = USUARIO_PADRAO_ID):

    """
    Executa uma única pergunta pelo grafo.

    O histórico de mensagens e o resumo da conversa do usuário são
    carregados do MongoDB no início da execução (node "memoria") e
    persistidos novamente ao final, caracterizando memória de longo
    prazo entre execuções distintas do chat de teste.
    """

    estado_inicial: GraphState = {
        "pergunta": pergunta,
        "resposta": "",
        "rota": "",
        "aprovado": False,
        "motivo_avaliacao": "",
        "tentativas": 0,
        "bloqueado": False,
        "categoria_bloqueio": "",
        "user_id": user_id,
        "conversation_id": None,
        "historico": [],
        "memory_context": None,
        "memorias_usuario": [],
        "execution_id": None
    }

    print("\n" + "=" * 60)
    print("KAIROS — TESTE DO GRAFO")
    print("=" * 60)

    print("\nPergunta:")
    print(pergunta)

    # Executa o fluxo completo.
    resultado = grafo.invoke(estado_inicial)

    print("\n" + "=" * 60)
    print("RESULTADO FINAL")
    print("=" * 60)

    print(f"\nGuardrail:")
    print("BLOQUEADO" if resultado["bloqueado"] else "LIBERADO")

    if resultado["bloqueado"]:
        print(f"Categoria: {resultado['categoria_bloqueio']}")

    print(f"\nRota escolhida:")
    print(resultado["rota"] or "(não roteado — mensagem bloqueada pelo guardrail)")

    print(f"\nTentativas:")
    print(resultado["tentativas"])

    print(f"\nJuiz:")
    if resultado["bloqueado"]:
        print("(não executado — mensagem bloqueada pelo guardrail)")
    else:
        print("APROVADO" if resultado["aprovado"] else "REPROVADO")
        print(f"\nMotivo da avaliação:")
        print(resultado["motivo_avaliacao"])

    print(f"\nResposta:")
    print(resultado["resposta"])

    print("\n" + "=" * 60)

    return resultado


def solicitar_feedback(resultado: dict) -> None:
    """
    Pergunta ao usuário, no chat de teste, se ele deseja avaliar a
    resposta recebida (nota de 1 a 5 e comentário opcional),
    persistindo a avaliação na collection `feedback`, vinculada à
    execução (`execution_id`) que gerou a resposta.

    Não solicita feedback quando a mensagem foi bloqueada pelo
    Guardrail, já que nesse caso não houve uma resposta de negócio
    para avaliar.
    """
    if resultado.get("bloqueado") or not resultado.get("execution_id"):
        return

    resposta_usuario = input(
        "\nDeseja avaliar essa resposta? (1-5, ou Enter para pular): "
    ).strip()

    if not resposta_usuario:
        return

    try:
        rating = int(resposta_usuario)
    except ValueError:
        print("[FEEDBACK] Nota inválida, feedback ignorado.")
        return

    if rating < 1 or rating > 5:
        print("[FEEDBACK] A nota deve estar entre 1 e 5. Feedback ignorado.")
        return

    comentario = input("Comentário (opcional, Enter para pular): ").strip() or None

    registrar_feedback(
        resultado["execution_id"], resultado["user_id"], rating, comentario
    )

    print("[FEEDBACK] Obrigado! Sua avaliação foi registrada.")


# EXECUÇÃO
COMANDOS_SAIDA = {"sair", "exit", "quit", "q"}

if __name__ == "__main__":

    print("\n" + "=" * 60)
    print("KAIROS — CHAT DE TESTE")
    print("=" * 60)
    print(
        "\nConverse com os agentes do Kairos. "
        "Digite 'sair' para encerrar."
    )
    print(
        "A conversa é persistida no MongoDB (memória de longo prazo). "
        f"Usuário de teste: {USUARIO_PADRAO_ID}."
    )

    while True:
        pergunta = input("\nVocê: ").strip()

        if not pergunta:
            continue

        if pergunta.lower() in COMANDOS_SAIDA:
            print("\nEncerrando o chat. Até a próxima!")
            break

        try:
            resultado = testar_pergunta(pergunta)
            solicitar_feedback(resultado)

        except KeyboardInterrupt:
            raise

        except Exception as erro:
            print(f"\n[ERRO] Ocorreu um problema ao processar a pergunta: {erro}")