"""Leitura de métricas LangSmith sem solicitar conteúdo ou credenciais das traces."""
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

from .generate import AGENTS, instant, number, period_bounds

# Projeção deliberada: não pede INPUTS, OUTPUTS, ERROR, EXTRA, METADATA ou URLs.
SELECTS = ["ID", "NAME", "STATUS", "RUN_TYPE", "START_TIME", "END_TIME",
           "LATENCY_SECONDS", "PARENT_RUN_IDS", "TRACE_ID", "TOTAL_COST", "TOTAL_TOKENS"]


def normalized(value):
    return str(value or "").upper()


async def bounded(iterator, maximum):
    records = []
    async for record in iterator:
        if len(records) >= maximum:
            raise ValueError("Limite do LangSmith atingido; nenhum relatório truncado foi gerado.")
        records.append(record)
    return records


def usage_confirmed(run):
    cost = number(run.total_cost)
    # O SDK pode preencher tokens=0/custo=0 em falhas sem uma medição de uso.
    if normalized(run.status) == "ERROR":
        return cost is not None and cost > 0
    return (cost is not None and normalized(run.status) != "PENDING"
            and (cost > 0 or number(run.total_tokens) is not None
                 or normalized(run.status) == "SUCCESS"))


def latency_ms(run):
    seconds = number(getattr(run, "latency_seconds", None))
    if seconds is not None:
        return seconds * 1000
    begin, finish = instant(run.start_time), instant(run.end_time)
    return ((finish - begin).total_seconds() * 1000
            if begin is not None and finish is not None and finish >= begin else None)


async def read_langsmith(client, project_name, start, end, maximum, graph_name="LangGraph"):
    import json
    lower, upper = period_bounds(start, end)
    project = await client.aread_project(project_name=project_name)
    # A janela de seleção pertence ao início da trace, não ao início de cada filho.
    scope = ('and(eq(name,' + json.dumps(graph_name) + '),gte(start_time,'
             + json.dumps(lower.isoformat()) + '),lt(start_time,' + json.dumps(upper.isoformat()) + '))')
    snapshots = await bounded(client.traces.query(
        project_id=str(project.id), min_start_time=lower, max_start_time=upper,
        trace_filter=scope, selects=SELECTS, page_size=100, timeout=15,
    ), maximum)
    roots = {}
    for snapshot in snapshots:
        root = snapshot.root_run
        if root is None or root.name != graph_name or root.parent_run_ids:
            continue
        when = instant(root.start_time)
        if when is not None and lower <= when < upper:
            roots[str(root.trace_id or root.id)] = snapshot
    if not roots:
        return [], [], {"model_calls": 0, "known_tokens": None, "calls_with_tokens": 0,
                        "outcomes_available": False}
    runs = await bounded(client.runs.query(
        project_ids=[str(project.id)], min_start_time=lower,
        max_start_time=max(upper, datetime.now(timezone.utc)), trace_filter=scope,
        selects=SELECTS, page_size=100, timeout=15,
    ), maximum)
    runs = list({str(run.id): run for run in runs}.values())
    grouped = defaultdict(list)
    for run in runs:
        key = str(run.trace_id or run.id)
        if key in roots:
            grouped[key].append(run)
    executions, traces = [], []
    model_calls = calls_with_tokens = known_tokens = 0
    for key, snapshot in roots.items():
        root = snapshot.root_run
        children = grouped[key]
        models = [run for run in children if normalized(run.run_type) in {"LLM", "EMBEDDING"}]
        model_calls += len(models)
        for run in models:
            tokens = number(run.total_tokens)
            if tokens is not None:
                calls_with_tokens += 1
                known_tokens += int(tokens)
        aggregate = snapshot.trace_aggregates
        # v2: o custo do run é próprio; o total da trace vem de trace_aggregates.
        total = number(aggregate.total_cost) if aggregate else None
        # Uma falha em ferramenta pode ocultar uso de embeddings/provedores não rastreados.
        complete = (normalized(root.status) == "SUCCESS" and bool(models)
                    and all(usage_confirmed(run) for run in models) and total is not None)
        known = total if total is not None and (total > 0 or complete) else None
        begin, finish = instant(root.start_time), instant(root.end_time)
        state = normalized(root.status)
        status = ("IN_PROGRESS" if finish is None or state == "PENDING" else
                  "SUCCESS" if state == "SUCCESS" else "ERROR" if state == "ERROR" else "UNKNOWN")
        executions.append({
            "id": key, "started_at": begin, "status": status,
            "total_latency_ms": latency_ms(root),
            "estimated_cost_usd": total if complete else None,
            "known_cost_usd": known, "cost_complete": complete,
        })
        for run in children:
            parents = [str(value) for value in (run.parent_run_ids or [])]
            # Só os filhos imediatos do grafo; LLMs e chains internas não viram agentes extras.
            if run.name not in AGENTS or parents != [str(root.id)]:
                continue
            begin, finish = instant(run.start_time), instant(run.end_time)
            traces.append({
                "id": str(run.id), "execution_id": key, "agent_name": run.name,
                "status": "ERROR" if normalized(run.status) == "ERROR" else "OK",
                "started_at": begin, "completed_at": finish,
                "latency_ms": latency_ms(run),
                "langsmith_run_id": str(run.id),
                # Não soma métricas de filhos e pais; custo fica na execução.
            })
    return executions, traces, {
        "model_calls": model_calls, "known_tokens": known_tokens if calls_with_tokens else None,
        "calls_with_tokens": calls_with_tokens, "outcomes_available": False,
    }


def load_langsmith(start, end, maximum, graph_name):
    import asyncio
    from data.langsmith_client import get_langsmith_client
    project = (os.getenv("LANGSMITH_PROJECT") or "").strip()
    if not project:
        raise ValueError("Configure LANGSMITH_PROJECT para selecionar o projeto correto.")
    # A CLI informa apenas erros sanitizados; logs do SDK podem conter URLs/fragmentos de chaves.
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(read_langsmith(get_langsmith_client(), project, start, end, maximum, graph_name))
    finally:
        logging.disable(previous)
