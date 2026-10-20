"""Identidade recebida de um backend confiável; nunca de uma mensagem do LLM."""
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ChatContext:
    user_id: int
    conversation_id: object
    request_id: str


chat_context: ContextVar[ChatContext | None] = ContextVar("chat_context", default=None)


def current_context():
    context = chat_context.get()
    if context is None:
        raise RuntimeError("A operação exige o contexto confiável da conversa.")
    return context
