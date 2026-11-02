"""Fallback por chamada de modelo, sem reexecutar ferramentas já concluídas."""
import logging

from langchain.agents.middleware import AgentMiddleware
from openai import RateLimitError

logger = logging.getLogger(__name__)


def com_fallback_de_limite(primary, fallback):
    """Não mascara erro de autenticação, validação ou indisponibilidade."""
    return primary.with_fallbacks([fallback], exceptions_to_handle=(RateLimitError,))


class GroqRateLimitFallback(AgentMiddleware):
    def __init__(self, fallback_model):
        self.fallback_model = fallback_model

    def wrap_model_call(self, request, handler):
        try:
            return handler(request)
        except RateLimitError:
            logger.warning("Limite da OpenAI atingido; usando Groq neste passo.")
            return handler(request.override(model=self.fallback_model))

    async def awrap_model_call(self, request, handler):
        try:
            return await handler(request)
        except RateLimitError:
            logger.warning("Limite da OpenAI atingido; usando Groq neste passo.")
            return await handler(request.override(model=self.fallback_model))
