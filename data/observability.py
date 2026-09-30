"""
Observabilidade e memória de longo prazo complementar do Kairos.

Este módulo é responsável por registrar, no MongoDB, as informações
de rastreamento (tracing) de cada execução do grafo de agentes,
complementando o histórico de conversa já tratado por
`data/memory_store.py`. As collections utilizadas são:

* ai_executions: uma execução completa do grafo (uma pergunta do
  usuário, do início ao fim).
* agent_traces: uma etapa (nó) executada dentro de uma execução do
  grafo (guardrail, orquestrador, especialista, juiz, memória).
* guardrail_events: decisão do Guardrail de Segurança sobre a
  mensagem do usuário de uma execução.
* judge_evaluations: decisão do Juiz sobre a resposta produzida por
  um especialista.
* feedback: avaliação (nota e comentário) dada pelo usuário sobre a
  resposta final de uma execução.
* user_memory: preferências e fatos sobre o usuário, extraídos ao
  longo das conversas, e reaproveitados como memória de longo prazo
  entre execuções distintas do chat.
"""

import threading
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId

from data.langsmith_client import buscar_metricas_run
from data.mongodb import get_database

NOME_EXECUCOES = "ai_executions"
NOME_TRACES = "agent_traces"
NOME_GUARDRAIL_EVENTS = "guardrail_events"
NOME_JUDGE_EVALUATIONS = "judge_evaluations"
NOME_FEEDBACK = "feedback"
NOME_USER_MEMORY = "user_memory"


def _agora() -> str:
    """
    Retorna o timestamp atual em UTC, no mesmo formato de string já
    utilizado pelos documentos de `data/memory_store.py`.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _agora_dt() -> datetime:
    """
    Retorna o instante atual em UTC como objeto datetime, utilizado
    para calcular a latência total de uma execução (`ai_executions`)
    a partir do seu horário de início.
    """
    return datetime.now(timezone.utc)


# ==================================================
# AI_EXECUTIONS
# ==================================================

def iniciar_execucao(user_id: int, conversation_id: ObjectId, pergunta: str) -> ObjectId:
    """
    Registra o início de uma nova execução do grafo de agentes (uma
    pergunta do usuário) na collection `ai_executions`, retornando o
    identificador (`execution_id`) que amarra os demais registros
    (traces, evento do guardrail, avaliação do juiz) a esta execução.
    """
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
    """
    Conclui uma execução iniciada por `iniciar_execucao`.

    A latência total é calculada a partir do horário de início
    registrado no próprio documento. A lista de agentes utilizados é
    agregada a partir dos traces (`agent_traces`) já registrados para
    esta execução no momento da chamada.

    O custo estimado é calculado com o que já estiver disponível
    nesse momento (`_atualizar_custo_execucao`), mas pode continuar
    sendo ajustado por mais alguns segundos depois: as métricas de
    cada trace são obtidas do LangSmith em segundo plano (ver
    `registrar_trace_agente`), então o custo final da execução só
    fica definitivo quando o último trace terminar de ser
    complementado.
    """
    colecao_execucoes = get_database()[NOME_EXECUCOES]
    colecao_traces = get_database()[NOME_TRACES]

    execucao = colecao_execucoes.find_one({"_id": execution_id})

    latencia_ms = None

    if execucao and execucao.get("started_at"):
        inicio = datetime.strptime(
            execucao["started_at"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
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


# ==================================================
# AGENT_TRACES
# ==================================================

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
    """
    Registra, na collection `agent_traces`, a execução de uma etapa
    (nó) do grafo de agentes dentro de uma execução específica.

    `langsmith_run_id` é o identificador do run correspondente no
    LangSmith (gerado pelo nó chamador ANTES do `.invoke()` e passado
    via `config={"run_id": ...}`, ver `gerar_run_id`). Quando
    informado, o trace é inserido imediatamente com os campos de
    modelo/tokens/custo em None, e a busca das métricas reais dessa
    chamada na API do LangSmith é delegada a uma thread em segundo
    plano (ver `_completar_metricas_em_segundo_plano`).

    Essa busca é assíncrona porque o LangSmith processa os runs de
    forma assíncrona: mesmo com `buscar_metricas_run` tentando
    algumas vezes antes de desistir, uma única chamada pode levar
    vários segundos para retornar. Bloquear cada nó do grafo por esse
    tempo tornaria o chat perceptivelmente mais lento a cada resposta;
    por isso o registro do trace nunca espera essas métricas — elas
    são preenchidas em `agent_traces` (e agregadas em `ai_executions`,
    via `_atualizar_custo_execucao`) posteriormente, sem impactar a
    resposta já entregue ao usuário.

    A latência do trace é sempre calculada localmente a partir de
    `started_at`/`completed_at`; diferente de tokens/custo, esse
    valor já é conhecido de imediato e não depende do LangSmith.

    Nós que não envolvem chamada a um modelo (ex.: `memoria_node`)
    não possuem run do LangSmith; nesse caso `langsmith_run_id` deve
    ser omitido e os campos de métricas permanecem None.
    """
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
    """
    Busca, em segundo plano, as métricas reais (modelo, tokens e
    custo em USD) de um run do LangSmith e as grava no trace já
    inserido por `registrar_trace_agente`, sem bloquear o fluxo do
    grafo. Ao final, também recalcula o custo agregado da execução em
    `ai_executions` (ver `_atualizar_custo_execucao`).

    Executada em uma thread separada (`daemon=True`): se o processo
    principal terminar antes de uma atualização concluir, essa thread
    é descartada silenciosamente, sem impedir o encerramento do chat.
    """
    metricas = buscar_metricas_run(langsmith_run_id)

    if metricas is None:
        # Métricas indisponíveis mesmo após as tentativas de
        # `buscar_metricas_run`; o trace permanece com os campos em
        # None, refletindo que o custo real não pôde ser confirmado.
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
    """
    Recalcula e persiste, em `ai_executions`, o custo total estimado
    de uma execução, somando `estimated_cost_usd` de todos os traces
    já registrados para ela em `agent_traces`.

    Chamada tanto por `finalizar_execucao` (com os traces disponíveis
    até aquele momento) quanto por
    `_completar_metricas_em_segundo_plano` (conforme as métricas de
    cada trace chegam do LangSmith, em segundo plano); por isso o
    custo de uma execução pode continuar sendo ajustado por um breve
    período após ela já ter sido finalizada.
    """
    colecao_execucoes = get_database()[NOME_EXECUCOES]
    colecao_traces = get_database()[NOME_TRACES]

    traces = list(colecao_traces.find({"execution_id": execution_id}))
    custo_total = sum(trace.get("estimated_cost_usd") or 0 for trace in traces)

    colecao_execucoes.update_one(
        {"_id": execution_id},
        {"$set": {"estimated_cost_usd": round(custo_total, 6)}}
    )


# ==================================================
# GUARDRAIL_EVENTS
# ==================================================

def registrar_evento_guardrail(
    execution_id: ObjectId, bloqueado: bool, categoria: str, motivo: str
) -> None:
    """
    Registra, na collection `guardrail_events`, a decisão do
    Guardrail de Segurança sobre a mensagem do usuário de uma
    execução.
    """
    colecao = get_database()[NOME_GUARDRAIL_EVENTS]

    colecao.insert_one({
        "execution_id": execution_id,
        "guardrail_type": "INPUT_VALIDATION",
        "action": "BLOCK" if bloqueado else "ALLOW",
        "category": categoria,
        "reason": motivo,
        "created_at": _agora()
    })


# ==================================================
# JUDGE_EVALUATIONS
# ==================================================

def registrar_avaliacao_juiz(execution_id: ObjectId, aprovado: bool, motivo: str) -> None:
    """
    Registra, na collection `judge_evaluations`, a decisão do Juiz
    sobre a resposta produzida pelo especialista de uma execução.
    """
    colecao = get_database()[NOME_JUDGE_EVALUATIONS]

    colecao.insert_one({
        "execution_id": execution_id,
        "judge_agent": "JUDGE_AGENT",
        "score": 1.0 if aprovado else 0.0,
        "hallucination_detected": not aprovado,
        "grounded": aprovado,
        "decision": "APPROVED" if aprovado else "REJECTED",
        "justification": motivo,
        "created_at": _agora()
    })


# ==================================================
# FEEDBACK
# ==================================================

def registrar_feedback(
    execution_id: ObjectId, user_id: int, rating: int, comment: Optional[str] = None
) -> None:
    """
    Registra, na collection `feedback`, a avaliação (nota de 1 a 5 e
    comentário opcional) dada pelo usuário sobre a resposta final de
    uma execução.
    """
    colecao = get_database()[NOME_FEEDBACK]

    colecao.insert_one({
        "execution_id": execution_id,
        "user_id": user_id,
        "rating": rating,
        "comment": comment,
        "created_at": _agora()
    })


# ==================================================
# USER_MEMORY
# ==================================================

def registrar_memorias(user_id: int, memorias: list[dict]) -> None:
    """
    Adiciona novas memórias de longo prazo (preferências ou fatos
    sobre o usuário) na collection `user_memory`, criando o
    documento do usuário caso ainda não exista.

    Cada item de `memorias` deve conter as chaves "tipo", "conteudo"
    e "importancia".
    """
    if not memorias:
        return

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
    """
    Carrega as memórias de longo prazo já registradas para o
    usuário, ordenadas das mais importantes para as menos
    importantes.
    """
    colecao = get_database()[NOME_USER_MEMORY]

    documento = colecao.find_one({"user_id": user_id})

    if not documento:
        return []

    memorias = documento.get("memories", [])
    return sorted(memorias, key=lambda m: m.get("importance", 0), reverse=True)