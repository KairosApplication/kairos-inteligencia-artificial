"""Lê métricas reais do LangSmith pelo run_id explícito de cada chamada.
Nomes de modelos vêm dos descendentes desse run; a ingestão pode ser assíncrona."""

import os
import time
import uuid
from typing import Optional, TypedDict

from dotenv import load_dotenv
from langsmith import Client

load_dotenv()

_client: Optional[Client] = None

# Aguarda a ingestão assíncrona das métricas pelo LangSmith.
TENTATIVAS_BUSCA_RUN = 5
INTERVALO_TENTATIVAS_SEGUNDOS = 1.0


class MetricasRun(TypedDict):
    """Métricas do LangSmith no formato dos traces persistidos."""
    model: Optional[str]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    total_tokens: Optional[int]
    total_cost_usd: Optional[float]
    latency_ms: Optional[int]
    status: Optional[str]


def get_langsmith_client() -> Client:
    """Reutiliza o cliente LangSmith e exige a chave configurada no ambiente."""
    global _client

    if _client is None:
        api_key = os.getenv("LANGSMITH_API_KEY")

        if not api_key:
            raise RuntimeError("LANGSMITH_API_KEY não foi configurada no .env.")

        _client = Client(
            api_url=os.getenv("LANGSMITH_ENDPOINT"),
            api_key=api_key
        )

    return _client


def gerar_run_id() -> str:
    """Gera o UUID passado em config.run_id para consultar esta chamada depois."""
    return str(uuid.uuid4())


def _buscar_nome_modelo(trace_id: str, dotted_order_raiz: Optional[str]) -> Optional[str]:
    """Busca o primeiro modelo nos descendentes do run, filtrados por dotted_order.
    Filtrar só por trace_id incluiria modelos de outros nós do mesmo turno."""
    if not dotted_order_raiz:
        return None

    cliente = get_langsmith_client()

    try:
        for run_filho in cliente.list_runs(trace_id=trace_id, run_type="llm"):
            e_descendente = (
                run_filho.dotted_order
                and run_filho.dotted_order.startswith(dotted_order_raiz)
            )

            if not e_descendente:
                continue

            nome_modelo = (run_filho.extra or {}).get("metadata", {}).get("ls_model_name")

            if nome_modelo:
                return nome_modelo

    except Exception:
        # Preserva tokens/custo do run raiz mesmo sem o nome do modelo.
        pass

    return None


def buscar_metricas_run(run_id: str) -> Optional[MetricasRun]:
    """Lê tokens, custo e latência do run com tentativas para aguardar a ingestão.
    Retorna None se indisponível; a falha de observabilidade não deve interromper o chat."""
    if not os.getenv("LANGSMITH_API_KEY"):
        return None
    cliente = get_langsmith_client()

    for tentativa in range(TENTATIVAS_BUSCA_RUN):
        try:
            run = cliente.read_run(run_id)

            # Tokens ausentes indicam que a agregação ainda pode estar pendente.
            if run.total_tokens is not None:
                latencia_ms = None

                # O SDK expõe os horários, sem um campo de latência pronto.
                if run.start_time is not None and run.end_time is not None:
                    latencia_ms = int(
                        (run.end_time - run.start_time).total_seconds() * 1000
                    )

                return MetricasRun(
                    model=_buscar_nome_modelo(
                        str(run.trace_id or run.id), run.dotted_order
                    ),
                    prompt_tokens=run.prompt_tokens,
                    completion_tokens=run.completion_tokens,
                    total_tokens=run.total_tokens,
                    total_cost_usd=(
                        float(run.total_cost) if run.total_cost is not None else None
                    ),
                    latency_ms=latencia_ms,
                    status=run.status
                )

        except Exception:
            # Ingestão pendente e falhas transitórias permitem nova tentativa.
            pass

        if tentativa < TENTATIVAS_BUSCA_RUN - 1:
            time.sleep(INTERVALO_TENTATIVAS_SEGUNDOS)

    return None
