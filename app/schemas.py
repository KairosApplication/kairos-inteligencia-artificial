from typing import Literal, TypedDict

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field, field_validator

from app.conversation_middleware import MemoryContext


class AvaliacaoGuardrail(BaseModel):
    """Decisão do guardrail de segurança."""

    bloqueado: bool = Field(description="Indica se a mensagem representa uma tentativa de manipulação e deve ser bloqueada.")

    categoria: Literal[
        "instrucao_maliciosa",
        "extracao_informacao_interna",
        "subversao_papel",
        "injecao_indireta",
        "acao_nao_autorizada",
        "nenhuma"
    ] = Field(description="Categoria de tentativa de manipulação identificada, ou 'nenhuma' se a mensagem for legítima.")

    motivo: str = Field(description="Motivo objetivo da decisão, sem repetir o conteúdo malicioso identificado.")


class Roteamento(BaseModel):
    """Especialista escolhido pelo orquestrador."""

    rota: Literal["faq", "estoque", "vendas", "clientes", "analista"] = Field(description="Categoria do especialista que deve atender à solicitação.")

    @field_validator("rota", mode="before")
    @classmethod
    def normalizar_rota(cls, valor):
        """Remove espaços e normaliza maiúsculas antes da validação da rota."""
        if isinstance(valor, str):
            return valor.strip().lower()
        return valor


class AvaliacaoJuiz(BaseModel):
    """Decisão do juiz sobre a resposta."""

    aprovado: bool = Field(description="Indica se a resposta pode ser entregue ao usuário.")

    motivo: str = Field(description="Motivo objetivo da decisão.")


class MemoriaExtraida(BaseModel):
    """Memória extraída de um turno aprovado."""

    tipo: Literal["preferencia", "fato", "restricao"] = Field(description="Categoria da memória extraída.")

    conteudo: str = Field(description="Descrição curta e objetiva da memória, independente do restante da conversa.")

    importancia: int = Field(description="Relevância da memória para atendimentos futuros, de 1 a 5.", ge=1, le=5)


class ExtracaoMemoria(BaseModel):
    """Memórias extraídas para atendimentos futuros."""

    memorias: list[MemoriaExtraida] = Field(description="Lista de memórias extraídas do turno. Pode ser vazia.")


class GraphState(TypedDict):
    pergunta: str
    resposta: str
    rota: str
    aprovado: bool
    motivo_avaliacao: str
    tentativas: int
    bloqueado: bool
    categoria_bloqueio: str
    # Contexto carregado pelo memoria_node.
    user_id: int
    conversation_id: object
    historico: list[BaseMessage]
    memory_context: MemoryContext
    memorias_usuario: list[dict]
    # Identificador compartilhado pelos traces do turno.
    execution_id: object
    evidencias: list[dict]
    sources: list[str]
