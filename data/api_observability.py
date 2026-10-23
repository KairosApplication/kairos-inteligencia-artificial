"""Persistência HTTP de observabilidade; mantém o formato interno do grafo."""
import logging
import threading
from datetime import datetime, timezone

from data.ai_api import get_ai_api
from data.access import principal
from data.langsmith_client import buscar_metricas_run

logger = logging.getLogger(__name__)


def iniciar_execucao(user_id, conversation_id, pergunta):
    return get_ai_api().create("ai-executions", {
        "user_id": user_id, "conversation_id": conversation_id, "input": pergunta,
        "status": "IN_PROGRESS", "agents_used": [],
    })["_id"]


def _instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=value.tzinfo or timezone.utc)


def finalizar_execucao(execution_id, status, resposta):
    api = get_ai_api()
    execution = api.get("ai-executions", execution_id)
    traces = list(api.list("agent-traces", executionId=str(execution_id)))
    now = datetime.now(timezone.utc)
    api.patch("ai-executions", execution_id, {
        "status": status, "final_response": resposta, "completed_at": now,
        "total_latency_ms": max(0, int((now - _instant(execution["started_at"])).total_seconds() * 1000)),
        "agents_used": [t["agent_name"] for t in traces],
    })
    _atualizar_custo(execution_id)


def _atualizar_custo(execution_id):
    api = get_ai_api()
    traces = [t for t in api.list("agent-traces", executionId=str(execution_id)) if t.get("langsmith_run_id")]
    complete = all(t.get("estimated_cost_usd") is not None for t in traces)
    known = round(sum(t.get("estimated_cost_usd") or 0 for t in traces), 6)
    api.patch("ai-executions", execution_id, {
        "estimated_cost_usd": known if complete else None,
        "known_cost_usd": known, "cost_complete": complete,
    })


def _completar_metricas(trace_id, execution_id, run_id):
    try:
        metrics = buscar_metricas_run(run_id)
        if metrics is None:
            return
        get_ai_api().patch("agent-traces", trace_id, {
            "model": metrics["model"], "prompt_tokens": metrics["prompt_tokens"],
            "completion_tokens": metrics["completion_tokens"], "estimated_tokens": metrics["total_tokens"],
            "estimated_cost_usd": round(metrics["total_cost_usd"], 6) if metrics["total_cost_usd"] is not None else None,
        })
        _atualizar_custo(execution_id)
    except Exception:
        logger.warning("Não foi possível complementar as métricas pela API de IA.")


def registrar_trace_agente(execution_id, agent_name, status, started_at, completed_at, entrada, saida, langsmith_run_id=None):
    trace = get_ai_api().create("agent-traces", {
        "execution_id": execution_id, "agent_name": agent_name, "status": status,
        "started_at": started_at, "completed_at": completed_at,
        "latency_ms": max(0, int((completed_at - started_at).total_seconds() * 1000)),
        "input": entrada, "output": saida, "langsmith_run_id": langsmith_run_id,
    })
    if langsmith_run_id:
        threading.Thread(target=_completar_metricas, args=(trace["_id"], execution_id, langsmith_run_id), daemon=True).start()


def registrar_evento_guardrail(execution_id, bloqueado, categoria, motivo):
    get_ai_api().create("guardrail-events", {
        "execution_id": execution_id, "guardrail_type": "INPUT_VALIDATION",
        "action": "BLOCK" if bloqueado else "ALLOW", "category": categoria, "reason": motivo,
    })


def registrar_avaliacao_juiz(execution_id, aprovado, motivo):
    get_ai_api().create("judge-evaluations", {
        "execution_id": execution_id, "judge_agent": "JUDGE_AGENT", "score": 1.0 if aprovado else 0.0,
        "grounded": True if aprovado else None, "hallucination_detected": None,
        "decision": "APPROVED" if aprovado else "REJECTED", "justification": motivo,
    })


def registrar_feedback(execution_id, user_id, rating, comment=None):
    get_ai_api().create("feedback", {"execution_id": execution_id, "user_id": user_id, "rating": rating, "comment": comment})


def registrar_memorias(user_id, memorias):
    if memorias:
        identity = principal()
        get_ai_api().append_memories(user_id, [
            {"type": m["tipo"], "content": m["conteudo"], "importance": m["importancia"],
             **({"access_scope": identity.scope} if identity else {})} for m in memorias
        ])


def carregar_memorias(user_id):
    # Matches the deterministic record selected by the runtime append operation.
    documents = get_ai_api().list("user-memories", userId=user_id)
    document = next(documents, {})
    identity = principal()
    memories = document.get("memories") or []
    if identity is not None:
        memories = [m for m in memories if m.get("access_scope") == identity.scope]
    return sorted(memories, key=lambda m: m.get("importance", 0), reverse=True)
