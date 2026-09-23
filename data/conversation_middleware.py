"""
Middleware de sumarização com persistência em memória de longo prazo.

Este módulo estende o `SummarizationMiddleware` do LangChain para que,
sempre que ele decidir comprimir o histórico de mensagens de uma
execução (por ter atingido o gatilho configurado em `trigger`), o
resumo gerado seja também persistido na coleção `conversations` do
MongoDB, via `data/memory_store.py`.

Sem essa extensão, o resumo do `SummarizationMiddleware` só existiria
durante a execução atual do agente (dentro do `state` do grafo) e
seria perdido ao final da chamada. Persistindo-o no MongoDB, o resumo
passa a servir como memória de longo prazo: turnos futuros (mesmo em
uma execução nova do processo) carregam esse resumo em vez do
histórico bruto completo, controlando o tamanho da janela de contexto
enviada ao modelo.
"""

from dataclasses import dataclass
from typing import Any, Optional

from bson import ObjectId
from langchain.agents.middleware.summarization import SummarizationMiddleware
from langgraph.runtime import Runtime

from data.memory_store import registrar_resumo

PREFIXO_RESUMO = "Here is a summary of the conversation to date:\n\n"


@dataclass
class MemoryContext:
    """
    Contexto estático repassado a cada chamada de `agent.invoke()`,
    necessário para que o `MongoSummarizationMiddleware` saiba em qual
    conversa persistir o resumo, e quantas mensagens brutas
    (carregadas do MongoDB no início do turno atual) o resumo cobre.
    """

    conversation_id: ObjectId
    # 1 se o turno atual foi iniciado com um resumo anterior já
    # carregado como a primeira mensagem do contexto, 0 caso
    # contrário.
    offset_summary: int
    # Quantidade de mensagens brutas (histórico já salvo no MongoDB)
    # carregadas como contexto inicial deste turno.
    offset_raw: int


class MongoSummarizationMiddleware(SummarizationMiddleware):
    """
    Igual ao `SummarizationMiddleware` do LangChain, mas também
    persiste o resumo gerado no MongoDB (memória de longo prazo),
    através do `MemoryContext` fornecido em tempo de execução.
    """

    def before_model(
        self, state: dict[str, Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        cutoff_index = self._calcular_cutoff_previsto(state, runtime)

        resultado = super().before_model(state, runtime)

        self._persistir_resumo_se_gerado(resultado, runtime, cutoff_index)

        return resultado

    async def abefore_model(
        self, state: dict[str, Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        cutoff_index = self._calcular_cutoff_previsto(state, runtime)

        resultado = await super().abefore_model(state, runtime)

        self._persistir_resumo_se_gerado(resultado, runtime, cutoff_index)

        return resultado

    def _calcular_cutoff_previsto(
        self, state: dict[str, Any], runtime: Runtime[Any]
    ) -> Optional[int]:
        """
        Calcula, antecipadamente, o índice de corte que o middleware
        base utilizará (caso decida sumarizar). Reaproveita a mesma
        lógica interna (`_determine_cutoff_index`) usada pelo
        `SummarizationMiddleware.before_model`, para saber depois
        quantas mensagens do contexto carregado do MongoDB foram
        efetivamente cobertas pelo resumo gerado.
        """
        if getattr(runtime, "context", None) is None:
            return None

        return self._determine_cutoff_index(state["messages"])

    def _persistir_resumo_se_gerado(
        self,
        resultado: dict[str, Any] | None,
        runtime: Runtime[Any],
        cutoff_index: Optional[int]
    ) -> None:
        if not resultado or cutoff_index is None:
            return

        contexto: MemoryContext = runtime.context

        resumo = self._extrair_texto_resumo(resultado)

        if resumo is None:
            return

        # Das mensagens carregadas do MongoDB no início do turno
        # (offset_summary + offset_raw), quantas ficaram antes do
        # ponto de corte (e portanto foram incorporadas ao resumo).
        mensagens_cobertas = max(
            0,
            min(cutoff_index - contexto.offset_summary, contexto.offset_raw)
        )

        registrar_resumo(contexto.conversation_id, resumo, mensagens_cobertas)

    @staticmethod
    def _extrair_texto_resumo(resultado: dict[str, Any]) -> Optional[str]:
        """
        Localiza, entre as mensagens retornadas pelo middleware, a
        mensagem de resumo (marcada com `lc_source=summarization`) e
        devolve apenas o texto do resumo, sem o prefixo padrão do
        LangChain.
        """
        for mensagem in resultado.get("messages", []):
            if mensagem.additional_kwargs.get("lc_source") == "summarization":
                return mensagem.content.removeprefix(PREFIXO_RESUMO)

        return None
