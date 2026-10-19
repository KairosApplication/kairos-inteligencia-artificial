from langgraph.graph import END, START, StateGraph
from functools import wraps
import logging

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
from config import _agora_dt
from data.observability import finalizar_execucao, registrar_trace_agente

logger = logging.getLogger(__name__)


def _node_observado(func):
    @wraps(func)
    def executar(state):
        inicio = _agora_dt()
        try:
            return func(state)
        except Exception as error:
            if state.get("execution_id"):
                try:
                    registrar_trace_agente(
                        state["execution_id"], func.__name__, "ERROR", inicio, _agora_dt(),
                        entrada={}, saida={"erro": type(error).__name__},
                    )
                    finalizar_execucao(state["execution_id"], "ERROR", "Não foi possível concluir a consulta.")
                except Exception:
                    logger.exception("Não foi possível registrar a falha de execução.")
            raise
    return executar

# CONSTRUÇÃO DO GRAFO
builder = StateGraph(GraphState)

# NODES
builder.add_node(
    "memoria",
    _node_observado(memoria_node)
)

builder.add_node(
    "guardrail",
    _node_observado(guardrail_node)
)

builder.add_node(
    "orquestrador",
    _node_observado(orquestrador_node)
)

builder.add_node(
    "faq",
    _node_observado(faq_node)
)

builder.add_node(
    "estoque",
    _node_observado(estoque_node)
)

builder.add_node(
    "vendas",
    _node_observado(vendas_node)
)

builder.add_node(
    "clientes",
    _node_observado(clientes_node)
)

builder.add_node(
    "juiz",
    _node_observado(juiz_node)
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
