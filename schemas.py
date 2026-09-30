from typing import Literal, TypedDict

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field, field_validator

from data.conversation_middleware import MemoryContext


# SCHEMAS DE SAÍDA ESTRUTURADA
class AvaliacaoGuardrail(BaseModel):
    """
    Define a saída esperada do Guardrail de Segurança.
    """

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
    """
    Define a saída esperada do Orquestrador.
    """

    rota: Literal["faq", "estoque", "vendas", "clientes"] = Field(description="Categoria do especialista que deve atender à solicitação.")

    @field_validator("rota", mode="before")
    @classmethod
    def normalizar_rota(cls, valor):
        """
        Normaliza a rota retornada pelo modelo (remove espaços e
        converte para minúsculas) antes da validação do Literal.

        A API da Groq não garante 100% de aderência ao schema
        (o modo `strict` não é suportado por todos os modelos), então
        o modelo eventualmente retorna a rota em maiúsculas (ex.:
        "FAQ") mesmo com o schema definindo apenas valores em
        minúsculas. Essa normalização evita que isso quebre o parser.
        """
        if isinstance(valor, str):
            return valor.strip().lower()
        return valor


class AvaliacaoJuiz(BaseModel):
    """
    Define a saída esperada do Juiz.
    """

    aprovado: bool = Field(description="Indica se a resposta pode ser entregue ao usuário.")

    motivo: str = Field(description="Motivo objetivo da decisão.")


class MemoriaExtraida(BaseModel):
    """
    Define uma única memória de longo prazo extraída de um turno.
    """

    tipo: Literal["preferencia", "fato", "restricao"] = Field(description="Categoria da memória extraída.")

    conteudo: str = Field(description="Descrição curta e objetiva da memória, independente do restante da conversa.")

    importancia: int = Field(description="Relevância da memória para atendimentos futuros, de 1 a 5.", ge=1, le=5)


class ExtracaoMemoria(BaseModel):
    """
    Define a saída esperada do Agente de Memória de Longo Prazo.
    """

    memorias: list[MemoriaExtraida] = Field(description="Lista de memórias extraídas do turno. Pode ser vazia.")


# ESTADO DO GRAFO
class GraphState(TypedDict):
    pergunta: str
    resposta: str
    rota: str
    aprovado: bool
    motivo_avaliacao: str
    tentativas: int
    bloqueado: bool
    categoria_bloqueio: str
    # Memória de longo prazo (preenchidos pelo memoria_node)
    user_id: int
    conversation_id: object
    historico: list[BaseMessage]
    memory_context: MemoryContext
    memorias_usuario: list[dict]
    # Observabilidade (preenchidos pelo memoria_node / consumidos por
    # todos os demais nós para registrar seus próprios traces)
    execution_id: object
