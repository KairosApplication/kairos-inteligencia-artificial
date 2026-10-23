"""Identidade recebida de um backend confiável; nunca de uma mensagem do LLM."""
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data.access import Principal


@dataclass(frozen=True)
class ChatContext:
    user_id: int
    conversation_id: object
    request_id: str
    principal: "Principal | None" = None


chat_context: ContextVar[ChatContext | None] = ContextVar("chat_context", default=None)


def current_context():
    context = chat_context.get()
    if context is None:
        raise RuntimeError("A operação exige o contexto confiável da conversa.")
    return context
