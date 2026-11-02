"""Persiste execuções, traces, avaliações, feedback e memórias de longo prazo."""

import threading
import logging
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId

from data.langsmith_client import buscar_metricas_run
from data.mongodb import get_database
from data.ai_api import via_api
from data import api_observability

logger = logging.getLogger(__name__)

NOME_EXECUCOES = "ai_executions"
NOME_TRACES = "agent_traces"
NOME_GUARDRAIL_EVENTS = "guardrail_events"
NOME_JUDGE_EVALUATIONS = "judge_evaluations"
NOME_FEEDBACK = "feedback"
NOME_USER_MEMORY = "user_memory"


def _agora() -> str:
    """Retorna o instante atual em UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _agora_dt() -> datetime:
    """Retorna o datetime UTC usado no cálculo de latência."""
    return datetime.now(timezone.utc)



def iniciar_execucao(user_id: int, conversation_id: ObjectId, pergunta: str) -> ObjectId:
    """Abre uma execução e retorna o ID que vincula seus traces e avaliações."""
    if via_api():
        return api_observability.iniciar_execucao(user_id, conversation_id, pergunta)
    colecao = get_database()[NOME_EXECUCOES]

    documento = {
        "conversation_id": conversation_id,
        "user_id": user_id,
        "input": pergunta,
        "status": "IN_PROGRESS",
        "started_at": _agora(),
        "completed_at": None,
        "total_latency_ms": None,
        "estimated_cost_usd": None,
        "final_response": None,
        "agents_used": []
    }

    resultado = colecao.insert_one(documento)
    return resultado.inserted_id


def finalizar_execucao(execution_id: ObjectId, status: str, resposta: str) -> None:
    """Fecha a execução, agregando latência, agentes e custos disponíveis.
    O custo pode ser atualizado quando as métricas do LangSmith chegarem."""
    if via_api():
        return api_observability.finalizar_execucao(execution_id, status, resposta)
    colecao_execucoes = get_database()[NOME_EXECUCOES]
    colecao_traces = get_database()[NOME_TRACES]

    execucao = colecao_execucoes.find_one({"_id": execution_id})

    latencia_ms = None

    if execucao and execucao.get("started_at"):
        inicio = execucao["started_at"]
        if isinstance(inicio, str):
            inicio = datetime.fromisoformat(inicio.replace("Z", "+00:00"))
        inicio = inicio.replace(tzinfo=inicio.tzinfo or timezone.utc)
        latencia_ms = int((_agora_dt() - inicio).total_seconds() * 1000)

    traces = list(
        colecao_traces.find({"execution_id": execution_id}).sort("started_at", 1)
    )

    agentes_utilizados = [trace["agent_name"] for trace in traces]

    colecao_execucoes.update_one(
        {"_id": execution_id},
        {
            "$set": {
                "status": status,
                "completed_at": _agora(),
                "total_latency_ms": latencia_ms,
                "final_response": resposta,
                "agents_used": agentes_utilizados
            }
        }
    )

    _atualizar_custo_execucao(execution_id)



def registrar_trace_agente(
    execution_id: ObjectId,
    agent_name: str,
    status: str,
    started_at: datetime,
    completed_at: datetime,
    entrada: dict,
    saida: dict,
    langsmith_run_id: Optional[str] = None
) -> None:
    """Persiste a etapa com latência local; busca métricas do run_id em segundo plano.
    O chamador gera esse ID antes do invoke. Sem run, modelo/tokens/custo ficam None."""
    if via_api():
        return api_observability.registrar_trace_agente(
            execution_id, agent_name, status, started_at, completed_at, entrada, saida, langsmith_run_id,
        )
    colecao = get_database()[NOME_TRACES]

    latencia_ms = int((completed_at - started_at).total_seconds() * 1000)

    resultado = colecao.insert_one({
        "execution_id": execution_id,
        "agent_name": agent_name,
        "status": status,
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "completed_at": completed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "latency_ms": latencia_ms,
        "input": entrada,
        "output": saida,
        "langsmith_run_id": langsmith_run_id,
        "model": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "estimated_tokens": None,
        "estimated_cost_usd": None
    })

    if langsmith_run_id:
        threading.Thread(
            target=_completar_metricas_em_segundo_plano,
            args=(resultado.inserted_id, execution_id, langsmith_run_id),
            daemon=True
        ).start()


def _completar_metricas_em_segundo_plano(
    trace_id: ObjectId, execution_id: ObjectId, langsmith_run_id: str
) -> None:
    """Atualiza métricas do trace e custo da execução sem bloquear o grafo.
    A thread daemon pode encerrar antes da atualização se o processo terminar."""
    try:
        metricas = buscar_metricas_run(langsmith_run_id)
    except Exception:
        logger.debug("Métricas LangSmith indisponíveis.", exc_info=True)
        return

    if metricas is None:
        # None preserva a distinção entre custo desconhecido e custo zero.
        return

    colecao_traces = get_database()[NOME_TRACES]

    colecao_traces.update_one(
        {"_id": trace_id},
        {
            "$set": {
                "model": metricas["model"],
                "prompt_tokens": metricas["prompt_tokens"],
                "completion_tokens": metricas["completion_tokens"],
                "estimated_tokens": metricas["total_tokens"],
                "estimated_cost_usd": (
                    round(metricas["total_cost_usd"], 6)
                    if metricas["total_cost_usd"] is not None
                    else None
                )
            }
        }
    )

    _atualizar_custo_execucao(execution_id)


def _atualizar_custo_execucao(execution_id: ObjectId) -> None:
    """Soma os custos disponíveis dos traces; pode atualizar uma execução já encerrada."""
    colecao_execucoes = get_database()[NOME_EXECUCOES]
    colecao_traces = get_database()[NOME_TRACES]

    traces = list(colecao_traces.find({"execution_id": execution_id}))
    traces_modelo = [trace for trace in traces if trace.get("langsmith_run_id")]
    completo = all(trace.get("estimated_cost_usd") is not None for trace in traces_modelo)
    custo_conhecido = sum(trace.get("estimated_cost_usd") or 0 for trace in traces_modelo)
    custo_total = round(custo_conhecido, 6) if completo else None

    colecao_execucoes.update_one(
        {"_id": execution_id},
        {"$set": {
            "estimated_cost_usd": custo_total,
            "known_cost_usd": round(custo_conhecido, 6),
            "cost_complete": completo,
        }}
    )



def registrar_evento_guardrail(
    execution_id: ObjectId, bloqueado: bool, categoria: str, motivo: str
) -> None:
    """Persiste a decisão do guardrail vinculada à execução."""
    if via_api():
        return api_observability.registrar_evento_guardrail(execution_id, bloqueado, categoria, motivo)
    colecao = get_database()[NOME_GUARDRAIL_EVENTS]

    colecao.insert_one({
        "execution_id": execution_id,
        "guardrail_type": "INPUT_VALIDATION",
        "action": "BLOCK" if bloqueado else "ALLOW",
        "category": categoria,
        "reason": motivo,
        "created_at": _agora()
    })



def registrar_avaliacao_juiz(execution_id: ObjectId, aprovado: bool, motivo: str) -> None:
    """Persiste a avaliação do juiz vinculada à execução."""
    if via_api():
        return api_observability.registrar_avaliacao_juiz(execution_id, aprovado, motivo)
    colecao = get_database()[NOME_JUDGE_EVALUATIONS]

    colecao.insert_one({
        "execution_id": execution_id,
        "judge_agent": "JUDGE_AGENT",
        "score": 1.0 if aprovado else 0.0,
        "hallucination_detected": None,
        "grounded": True if aprovado else None,
        "decision": "APPROVED" if aprovado else "REJECTED",
        "justification": motivo,
        "created_at": _agora()
    })



def registrar_feedback(
    execution_id: ObjectId, user_id: int, rating: int, comment: Optional[str] = None
) -> None:
    """Persiste nota de 1 a 5 e comentário opcional para a execução."""
    if via_api():
        return api_observability.registrar_feedback(execution_id, user_id, rating, comment)
    colecao = get_database()[NOME_FEEDBACK]

    colecao.insert_one({
        "execution_id": execution_id,
        "user_id": user_id,
        "rating": rating,
        "comment": comment,
        "created_at": _agora()
    })



def registrar_memorias(user_id: int, memorias: list[dict]) -> None:
    """Adiciona memórias com tipo, conteudo e importancia ao usuário."""
    if not memorias:
        return

    if via_api():
        return api_observability.registrar_memorias(user_id, memorias)
    colecao = get_database()[NOME_USER_MEMORY]

    novas_memorias = [
        {
            "memory_id": ObjectId(),
            "type": memoria["tipo"],
            "content": memoria["conteudo"],
            "importance": memoria["importancia"],
            "created_at": _agora(),
            "updated_at": _agora()
        }
        for memoria in memorias
    ]

    colecao.update_one(
        {"user_id": user_id},
        {
            "$push": {"memories": {"$each": novas_memorias}},
            "$set": {"updated_at": _agora()},
            "$setOnInsert": {"user_id": user_id}
        },
        upsert=True
    )


def carregar_memorias(user_id: int) -> list[dict]:
    """Carrega memórias do usuário em ordem decrescente de importância."""
    if via_api():
        return api_observability.carregar_memorias(user_id)
    colecao = get_database()[NOME_USER_MEMORY]

    documento = colecao.find_one({"user_id": user_id})

    if not documento:
        return []

    memorias = documento.get("memories", [])
    return sorted(memorias, key=lambda m: m.get("importance", 0), reverse=True)
