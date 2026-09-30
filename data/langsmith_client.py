"""
Cliente LangSmith e leitura de runs do Kairos.

Este módulo isola o acesso ao LangSmith (API de observabilidade do
LangChain), usado por `data/observability.py` para obter as métricas
reais de cada chamada de modelo (tokens, custo estimado em USD e
latência), em vez de calculá-las manualmente no código.

As credenciais (`LANGSMITH_API_KEY`, `LANGSMITH_ENDPOINT`,
`LANGSMITH_PROJECT`) vêm inteiramente do `.env`; nada aqui é
hard-coded.

Como capturar o run correto
----------------------------
`get_current_run_tree()` só retorna um run quando chamado DENTRO do
escopo de execução de uma chamada rastreada; como os nós do grafo
chamam essa função depois que `.invoke()` já retornou, ela acaba
capturando o run do nó do LangGraph (um "chain" pai), não o run do
LLM propriamente dito. Por isso este módulo usa a abordagem
recomendada pelo LangSmith para saber o `run_id` de antemão: cada
nó gera um UUID com `gerar_run_id()` e o passa via
`config={"run_id": ...}` no `.invoke()`; o run raiz daquela chamada
específica passa a ter exatamente esse ID.

Estrutura de uma chamada com `with_structured_output(include_raw=True)`
-------------------------------------------------------------------------
Uma única chamada desse tipo (usada pelo Guardrail, Juiz e Agente de
Memória) gera uma árvore de vários runs internos do LangChain
(RunnableSequence, RunnableParallel, PydanticOutputParser etc.), com
apenas um run do tipo "llm" (o `ChatGroq` de fato) em algum nível
abaixo do run raiz. Tokens e custo já aparecem corretamente
agregados no run raiz (identificado pelo `run_id` explícito), mas o
nome do modelo (`ls_model_name`) só é setado no run-filho do tipo
"llm"; por isso `buscar_metricas_run` faz uma segunda consulta
(`_buscar_nome_modelo`) para encontrá-lo.

Como todos os nós de um turno (guardrail, orquestrador,
especialista, juiz, extração de memória) rodam dentro de uma ÚNICA
trace do LangSmith (o turno inteiro é uma trace, não cada nó), essa
segunda consulta não pode filtrar apenas por `trace_id` — isso
retornaria os runs "llm" de TODOS os nós do turno, não só os da
chamada em questão. Por isso o filtro usa `dotted_order` (a string
com que o LangSmith codifica a posição de um run na árvore,
incluindo o caminho de ancestrais) para manter apenas os runs
descendentes do run raiz da chamada específica.

Importante: o LangSmith registra os runs de forma ASSÍNCRONA. No
momento em que um nó do grafo termina de chamar `.invoke()`, os
campos agregados de tokens/custo do run correspondente podem levar
alguns segundos para ficarem disponíveis via API. Por isso,
`buscar_metricas_run` tenta algumas vezes, com espera entre elas,
antes de desistir.
"""

import os
import time
import uuid
from typing import Optional, TypedDict

from dotenv import load_dotenv
from langsmith import Client

load_dotenv()

# Cliente único, criado sob demanda (lazy) e reutilizado entre
# chamadas, no mesmo padrão de `data/mongodb.py`.
_client: Optional[Client] = None

# Número de tentativas e intervalo (segundos) entre elas ao buscar as
# métricas de um run recém-criado, que ainda pode não ter sido
# totalmente processado pelo LangSmith.
TENTATIVAS_BUSCA_RUN = 5
INTERVALO_TENTATIVAS_SEGUNDOS = 1.0


class MetricasRun(TypedDict):
    """
    Métricas de um run do LangSmith relevantes para a observabilidade
    do Kairos, já no formato utilizado por `agent_traces`.
    """
    model: Optional[str]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    total_tokens: Optional[int]
    total_cost_usd: Optional[float]
    latency_ms: Optional[int]
    status: Optional[str]


def get_langsmith_client() -> Client:
    """
    Obtém o cliente do LangSmith, criando-o na primeira chamada.

    O próprio `Client` do LangSmith já lê `LANGSMITH_API_KEY` e
    `LANGSMITH_ENDPOINT` do ambiente; eles são repassados
    explicitamente aqui apenas para falhar de forma clara quando a
    chave não estiver configurada, em vez de uma falha silenciosa
    mais adiante.
    """
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
    """
    Gera um identificador de run a ser passado explicitamente para um
    `.invoke()` (via `config={"run_id": ...}`), permitindo consultar
    depois, com certeza, o run correspondente a essa chamada
    específica no LangSmith — em vez de depender de
    `get_current_run_tree()`, que só é confiável enquanto a chamada
    ainda está em andamento.
    """
    return str(uuid.uuid4())


def _buscar_nome_modelo(trace_id: str, dotted_order_raiz: Optional[str]) -> Optional[str]:
    """
    Busca o nome do modelo (`ls_model_name`) entre os runs
    DESCENDENTES do run raiz de uma chamada específica (identificado
    por `dotted_order_raiz`), procurando o primeiro run do tipo "llm".

    Necessário porque, em chamadas com `with_structured_output`, o
    run raiz (identificado pelo `run_id` explícito) é um "chain" que
    engloba vários runs internos do LangChain; apenas o run do tipo
    "llm", em algum nível abaixo dele, tem esse metadado.

    Importante: o Kairos executa todo o grafo (todos os nós de um
    turno) dentro de uma ÚNICA trace do LangSmith, então
    `list_runs(trace_id=...)` retorna os runs "llm" de TODOS os nós
    do turno (guardrail, orquestrador, especialista, juiz etc.), não
    apenas os da chamada em questão. `dotted_order` é a string que o
    LangSmith usa para codificar a posição de um run na árvore
    (inclui o caminho de ancestrais); um run é descendente do run
    raiz somente quando seu `dotted_order` começa com o dele. Sem
    esse filtro, o primeiro run "llm" da trace inteira seria
    retornado, mesmo que pertencesse a outro nó do grafo.
    """
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
        # Indisponibilidade momentânea da busca por runs filhos não
        # deve impedir o registro das demais métricas (tokens/custo),
        # já obtidas do run raiz; o nome do modelo apenas fica None.
        pass

    return None


def buscar_metricas_run(run_id: str) -> Optional[MetricasRun]:
    """
    Busca, na API do LangSmith, as métricas agregadas de um run
    (tokens, custo estimado e latência), identificado por `run_id`
    (o mesmo UUID gerado por `gerar_run_id` e passado ao `.invoke()`
    original via `config={"run_id": ...}`).

    Como o LangSmith processa os runs de forma assíncrona, os campos
    de tokens/custo podem ainda não existir nos primeiros instantes
    após o término da chamada. Por isso, tenta a leitura algumas
    vezes (`TENTATIVAS_BUSCA_RUN`), aguardando
    `INTERVALO_TENTATIVAS_SEGUNDOS` entre tentativas, até que o total
    de tokens seja preenchido.

    Retorna None se o run não puder ser lido em nenhuma tentativa
    (ex.: problema de rede, run inexistente); o chamador deve tratar
    isso como "métricas indisponíveis por enquanto", sem interromper
    o fluxo do chat.
    """
    cliente = get_langsmith_client()

    for tentativa in range(TENTATIVAS_BUSCA_RUN):
        try:
            run = cliente.read_run(run_id)

            # `total_tokens` só é preenchido pelo LangSmith depois que
            # o run é totalmente processado. Enquanto for None,
            # ainda vale a pena tentar de novo (run existe, mas as
            # métricas agregadas ainda não chegaram).
            if run.total_tokens is not None:
                latencia_ms = None

                # O objeto `Run` do SDK Python expõe `start_time` e
                # `end_time` (datetimes), não um `latency_seconds`
                # pronto (esse campo só existe na resposta JSON crua
                # da REST API); por isso a latência é calculada aqui.
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
            # Run ainda pode não existir na primeira tentativa (delay
            # de ingestão) ou ocorrer um erro de rede pontual; ambos
            # os casos justificam nova tentativa.
            pass

        if tentativa < TENTATIVAS_BUSCA_RUN - 1:
            time.sleep(INTERVALO_TENTATIVAS_SEGUNDOS)

    return None
