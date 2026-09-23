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

from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from langchain_core.messages import AIMessage, BaseMessage

from data.mongodb import get_database

NOME_EXECUCOES = "ai_executions"
NOME_TRACES = "agent_traces"
NOME_GUARDRAIL_EVENTS = "guardrail_events"
NOME_JUDGE_EVALUATIONS = "judge_evaluations"
NOME_FEEDBACK = "feedback"
NOME_USER_MEMORY = "user_memory"

# Preço por token (USD), conforme a tabela pública de modelos da Groq
# (https://console.groq.com/docs/models). Usado apenas para uma
# estimativa interna de custo por execução; não substitui a fatura
# real emitida pela Groq.
PRECOS_POR_MODELO = {
    "openai/gpt-oss-120b": {"input": 0.15 / 1_000_000, "output": 0.60 / 1_000_000},
    "openai/gpt-oss-20b": {"input": 0.075 / 1_000_000, "output": 0.30 / 1_000_000},
}


def _agora() -> str:
    """
    Retorna o timestamp atual em UTC, no mesmo formato de string já
    utilizado pelos documentos de `data/memory_store.py`.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _agora_dt() -> datetime:
    """
    Retorna o instante atual em UTC como objeto datetime, utilizado
    apenas para cálculo de latência dentro deste módulo.
    """
    return datetime.now(timezone.utc)


def calcular_custo_usd(
    model: Optional[str], input_tokens: int, output_tokens: int
) -> Optional[float]:
    """
    Estima o custo em USD de uma chamada ao modelo, com base na
    tabela de preços conhecida da Groq. Retorna None quando o modelo
    informado não está mapeado na tabela (custo desconhecido).
    """
    precos = PRECOS_POR_MODELO.get(model)

    if not precos:
        return None

    return input_tokens * precos["input"] + output_tokens * precos["output"]


def extrair_uso_mensagens(mensagens: list[BaseMessage]) -> tuple[int, int, int]:
    """
    Soma os tokens de entrada, saída e total relatados pelo modelo em
    todas as AIMessage presentes na lista informada.

    Uma única execução de um agente (create_agent) pode gerar mais de
    uma AIMessage quando há chamadas de ferramenta intermediárias;
    somamos o uso de todas elas para refletir o custo real do turno.

    Retorna (tokens_entrada, tokens_saida, tokens_total).
    """
    total_entrada = 0
    total_saida = 0
    total = 0

    for mensagem in mensagens:
        if isinstance(mensagem, AIMessage) and mensagem.usage_metadata:
            total_entrada += mensagem.usage_metadata.get("input_tokens") or 0
            total_saida += mensagem.usage_metadata.get("output_tokens") or 0
            total += mensagem.usage_metadata.get("total_tokens") or 0

    return total_entrada, total_saida, total


def extrair_uso_mensagem(mensagem: Optional[BaseMessage]) -> tuple[int, int, int]:
    """
    Mesma lógica de `extrair_uso_mensagens`, mas para uma única
    mensagem. Utilizado pelos nós que fazem uma única chamada ao
    modelo com saída estruturada (Guardrail, Orquestrador, Juiz e
    Agente de Memória), quando invocados com `include_raw=True`.
    """
    if mensagem is None:
        return 0, 0, 0

    return extrair_uso_mensagens([mensagem])


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
    registrado no próprio documento. O custo estimado e a lista de
    agentes utilizados são agregados a partir dos traces
    (`agent_traces`) já registrados para esta execução no momento da
    chamada.
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
    custo_total = sum(trace.get("estimated_cost_usd") or 0 for trace in traces)

    colecao_execucoes.update_one(
        {"_id": execution_id},
        {
            "$set": {
                "status": status,
                "completed_at": _agora(),
                "total_latency_ms": latencia_ms,
                "estimated_cost_usd": round(custo_total, 6),
                "final_response": resposta,
                "agents_used": agentes_utilizados
            }
        }
    )


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
    model: Optional[str] = None,
    estimated_tokens: Optional[int] = None,
    estimated_cost_usd: Optional[float] = None
) -> None:
    """
    Registra, na collection `agent_traces`, a execução de uma etapa
    (nó) do grafo de agentes dentro de uma execução específica.
    """
    colecao = get_database()[NOME_TRACES]

    colecao.insert_one({
        "execution_id": execution_id,
        "agent_name": agent_name,
        "status": status,
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "completed_at": completed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "latency_ms": int((completed_at - started_at).total_seconds() * 1000),
        "input": entrada,
        "output": saida,
        "model": model,
        "estimated_tokens": estimated_tokens,
        "estimated_cost_usd": (
            round(estimated_cost_usd, 6) if estimated_cost_usd is not None else None
        )
    })


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