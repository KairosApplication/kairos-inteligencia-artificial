import os
from typing import TypedDict, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from langchain_groq import ChatGroq
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, START, END
from prompts import (ORQUESTRADOR_PROMPT, FAQ_PROMPT, ESTOQUE_PROMPT, VENDAS_PROMPT, CLIENTES_PROMPT, JUIZ_PROMPT)
from tools.faq import FAQ_TOOLS
from tools.estoque import ESTOQUE_TOOLS
from tools.vendas import VENDAS_TOOLS
from tools.clientes import CLIENTES_TOOLS

# CONFIGURAÇÃO
load_dotenv()

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
class Roteamento(BaseModel):
    """
    Define a saída esperada do Orquestrador.
    """

    rota: Literal["faq", "estoque", "vendas", "clientes"] = Field(description="Categoria do especialista que deve atender à solicitação.")

class AvaliacaoJuiz(BaseModel):
    """
    Define a saída esperada do Juiz.
    """

    aprovado: bool = Field(description="Indica se a resposta pode ser entregue ao usuário.")

    motivo: str = Field(description="Motivo objetivo da decisão.")


# ORQUESTRADOR
# O Orquestrador não é um agente com tools.
# Ele apenas classifica a intenção e retorna uma rota estruturada.
orquestrador = llm_rapido.with_structured_output(
    Roteamento,
    method="json_schema",
    strict=True
)

# AGENTE FAQ
agente_faq = create_agent(
    model=llm_rapido,
    tools=FAQ_TOOLS,
    system_prompt=FAQ_PROMPT
)

# AGENTE ESTOQUE
agente_estoque = create_agent(
    model=llm_groq,
    tools=ESTOQUE_TOOLS,
    system_prompt=ESTOQUE_PROMPT
)

# AGENTE VENDAS
agente_vendas = create_agent(
    model=llm_groq,
    tools=VENDAS_TOOLS,
    system_prompt=VENDAS_PROMPT
)

# AGENTE CLIENTES
agente_clientes = create_agent(
    model=llm_groq,
    tools=CLIENTES_TOOLS,
    system_prompt=CLIENTES_PROMPT
)

# JUIZ
# Assim como o Orquestrador, o Juiz não precisa de tools.
# Ele apenas avalia a resposta e retorna uma decisão estruturada.
juiz = llm_groq.with_structured_output(
    AvaliacaoJuiz,
    method="json_schema",
    strict=True
)

# ESTADO DO GRAFO
class GraphState(TypedDict):
    pergunta: str
    resposta: str
    rota: str
    aprovado: bool
    motivo_avaliacao: str
    tentativas: int

# NODE ORQUESTRADOR
def orquestrador_node(state: GraphState):
    pergunta = state["pergunta"]

    resultado = orquestrador.invoke(
        ORQUESTRADOR_PROMPT
        + "\n\nPergunta do usuário:\n"
        + pergunta
    )
    
    return {"rota": resultado.rota}


# NODE FAQ
def faq_node(state: GraphState):
    pergunta = state["pergunta"]

    resultado = agente_faq.invoke({
        "messages": [
            HumanMessage(content=pergunta)
        ]
    })

    resposta = resultado["messages"][-1].content

    # Incrementa o número de tentativas.
    tentativas = state.get("tentativas", 0) + 1


    return {
        "resposta": resposta,
        "tentativas": tentativas
    }


# NODE ESTOQUE
def estoque_node(state: GraphState):
    pergunta = state["pergunta"]

    resultado = agente_estoque.invoke({
        "messages": [
            HumanMessage(content=pergunta)
        ]
    })

    resposta = resultado["messages"][-1].content

    # Incrementa o número de tentativas.
    tentativas = state.get("tentativas", 0) + 1

    return {
        "resposta": resposta,
        "tentativas": tentativas
    }


# NODE VENDAS
def vendas_node(state: GraphState):
    pergunta = state["pergunta"]

    resultado = agente_vendas.invoke({
        "messages": [
            HumanMessage(content=pergunta)
        ]
    })

    resposta = resultado["messages"][-1].content

    # Incrementa o número de tentativas.
    tentativas = state.get("tentativas", 0) + 1

    return {
        "resposta": resposta,
        "tentativas": tentativas
    }


# NODE CLIENTES
def clientes_node(state: GraphState):
    pergunta = state["pergunta"]

    resultado = agente_clientes.invoke({
        "messages": [
            HumanMessage(content=pergunta)
        ]
    })

    resposta = resultado["messages"][-1].content

    # Incrementa o número de tentativas.
    tentativas = state.get("tentativas", 0) + 1

    return {
        "resposta": resposta,
        "tentativas": tentativas
    }


# NODE - JUIZ
def juiz_node(state: GraphState):
    pergunta = state["pergunta"]
    resposta = state["resposta"]

    prompt_avaliacao = f"""
Pergunta original do usuário: {pergunta}

Resposta produzida pelo agente especialista: {resposta}

Avalie se a resposta produzida pode ser entregue ao usuário.
"""

    resultado = juiz.invoke(
        JUIZ_PROMPT
        + "\n\n"
        + prompt_avaliacao
    )

    print("[JUIZ] Resultado: " + ("APROVADO" if resultado.aprovado else "REPROVADO"))

    print(f"[JUIZ] Motivo: {resultado.motivo}")

    return {
        "aprovado": resultado.aprovado,
        "motivo_avaliacao": resultado.motivo
    }


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
    "orquestrador"
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
def testar_pergunta(pergunta: str):

    """
    Executa uma única pergunta pelo grafo.

    Cada chamada cria um estado novo.
    Não existe memória ou histórico entre execuções.
    """

    estado_inicial: GraphState = {
        "pergunta": pergunta,
        "resposta": "",
        "rota": "",
        "aprovado": False,
        "motivo_avaliacao": "",
        "tentativas": 0
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

    print(f"\nRota escolhida:")
    print(resultado["rota"])

    print(f"\nTentativas:")
    print(resultado["tentativas"])

    print(f"\nJuiz:")
    print(
        "APROVADO"
        if resultado["aprovado"]
        else "REPROVADO"
    )

    print(f"\nMotivo da avaliação:")
    print(resultado["motivo_avaliacao"])

    print(f"\nResposta:")
    print(resultado["resposta"])

    print("\n" + "=" * 60)

    return resultado

# EXECUÇÃO
if __name__ == "__main__":

    pergunta = input(
        "Digite sua pergunta para o Kairos: "
    ).strip()

    if not pergunta:
        print("Nenhuma pergunta foi informada.")

    else:
        testar_pergunta(pergunta)