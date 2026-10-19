from bson import ObjectId
from langchain_core.messages import BaseMessage, HumanMessage

from agents import agente_clientes, agente_estoque, agente_faq, agente_orquestrador, agente_vendas
from config import USUARIO_PADRAO_ID, _agora_dt
from data.memory_store import (
    carregar_mensagens_contexto,
    contar_mensagens_brutas_carregadas,
    obter_ou_criar_conversa_ativa,
    registrar_turno
)
from data.conversation_middleware import MemoryContext
from data.langsmith_client import gerar_run_id
from data.observability import (
    carregar_memorias,
    finalizar_execucao,
    iniciar_execucao,
    registrar_avaliacao_juiz,
    registrar_evento_guardrail,
    registrar_memorias,
    registrar_trace_agente
)
from llms import extrator_memoria, guardrail, juiz
from prompts import GUARDRAIL_PROMPT, JUIZ_PROMPT, MEMORIA_PROMPT
from schemas import AvaliacaoGuardrail, AvaliacaoJuiz, ExtracaoMemoria, GraphState, Roteamento


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

    langsmith_run_id = gerar_run_id()

    saida = guardrail.invoke(
        GUARDRAIL_PROMPT
        + "\n\nMensagem do usuário:\n"
        + pergunta,
        config={"run_id": langsmith_run_id}
    )

    resultado: AvaliacaoGuardrail = saida["parsed"]

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
        langsmith_run_id=langsmith_run_id
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

    langsmith_run_id = gerar_run_id()

    resultado_agente = agente_orquestrador.invoke(
        {"messages": [HumanMessage(content=pergunta)]},
        config={"run_id": langsmith_run_id}
    )

    resultado: Roteamento = resultado_agente["structured_response"]

    registrar_trace_agente(
        execution_id,
        "orquestrador",
        "OK",
        inicio,
        _agora_dt(),
        entrada={"pergunta": pergunta},
        saida={"rota": resultado.rota},
        langsmith_run_id=langsmith_run_id
    )

    return {"rota": resultado.rota}


def _executar_especialista(nome_agente: str, agente, state: GraphState) -> dict:
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
    langsmith_run_id = gerar_run_id()

    resultado = agente.invoke(
        {"messages": mensagens_entrada},
        context=state["memory_context"],
        config={"run_id": langsmith_run_id}
    )

    mensagens_saida = resultado["messages"]
    resposta = mensagens_saida[-1].content

    registrar_trace_agente(
        state["execution_id"],
        nome_agente,
        "OK",
        inicio,
        _agora_dt(),
        entrada={"pergunta": pergunta},
        saida={"resposta": resposta},
        langsmith_run_id=langsmith_run_id
    )

    # Incrementa o número de tentativas.
    tentativas = state.get("tentativas", 0) + 1

    return {
        "resposta": resposta,
        "tentativas": tentativas
    }


# NODE FAQ
def faq_node(state: GraphState):
    return _executar_especialista("faq", agente_faq, state)


# NODE ESTOQUE
def estoque_node(state: GraphState):
    return _executar_especialista("estoque", agente_estoque, state)


# NODE VENDAS
def vendas_node(state: GraphState):
    return _executar_especialista("vendas", agente_vendas, state)


# NODE CLIENTES
def clientes_node(state: GraphState):
    return _executar_especialista("clientes", agente_clientes, state)


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

    langsmith_run_id = gerar_run_id()

    saida = juiz.invoke(
        JUIZ_PROMPT
        + "\n\n"
        + prompt_avaliacao,
        config={"run_id": langsmith_run_id}
    )

    resultado: AvaliacaoJuiz = saida["parsed"]

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
        langsmith_run_id=langsmith_run_id
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

    langsmith_run_id = gerar_run_id()

    try:
        saida = extrator_memoria.invoke(
            MEMORIA_PROMPT
            + "\n\n"
            + prompt_extracao,
            config={"run_id": langsmith_run_id}
        )

        resultado: ExtracaoMemoria = saida["parsed"]

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
            langsmith_run_id=langsmith_run_id
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
