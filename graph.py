from langgraph.graph import END, START, StateGraph
from functools import wraps
import logging

from graph_nodes import (
    analista_node,
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

builder = StateGraph(GraphState)

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

builder.add_node("analista", _node_observado(analista_node))

builder.add_edge(
    START,
    "memoria"
)

builder.add_edge(
    "memoria",
    "guardrail"
)

builder.add_conditional_edges(
    "guardrail",
    decidir_bloqueio,
    {
        "orquestrador": "orquestrador",
        "fim": END
    }
)

builder.add_conditional_edges(
    "orquestrador",
    decidir_rota,
    {
        "faq": "faq",
        "estoque": "estoque",
        "vendas": "vendas",
        "clientes": "clientes",
        "analista": "analista"
    }
)

builder.add_edge(
    "faq",
    "juiz"
)

builder.add_edge(
    "estoque",
    "juiz"
)

builder.add_edge(
    "vendas",
    "juiz"
)

builder.add_edge(
    "clientes",
    "juiz"
)

builder.add_edge("analista", "juiz")

builder.add_conditional_edges(
    "juiz",
    decidir_avaliacao,
    {
        "fim": END,
        "faq": "faq",
        "estoque": "estoque",
        "vendas": "vendas",
        "clientes": "clientes",
        "analista": "analista"
    }
)

grafo = builder.compile()
