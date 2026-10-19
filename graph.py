from langgraph.graph import END, START, StateGraph

from graph_nodes import (
    clientes_node,
    decidir_avaliacao,
    decidir_bloqueio,
    decidir_rota,
    estoque_node,
    faq_node,
    guardrail_node,
    juiz_node,
    memoria_node,
    orquestrador_node,
    vendas_node
)
from schemas import GraphState

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
