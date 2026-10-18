from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from langchain_core.runnables import RunnableLambda
from openai import AuthenticationError, RateLimitError

from data.model_fallback import GroqRateLimitFallback, com_fallback_de_limite


def rate_limit():
    return RateLimitError(
        "rate limit",
        response=httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")),
        body={"error": {"code": "rate_limit_exceeded"}},
    )


def test_fallback_somente_quando_openai_limita():
    primary = Mock(side_effect=rate_limit())
    backup = Mock(return_value="groq")
    chain = com_fallback_de_limite(RunnableLambda(primary), RunnableLambda(backup))
    assert chain.invoke("pergunta") == "groq"
    backup.assert_called_once_with("pergunta")


def test_sucesso_nao_aciona_groq():
    backup = Mock()
    chain = com_fallback_de_limite(RunnableLambda(lambda _: "openai"), RunnableLambda(backup))
    assert chain.invoke("pergunta") == "openai"
    backup.assert_not_called()


def test_falha_de_autenticacao_nao_e_mascarada():
    error = AuthenticationError("invalid key", response=httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com")), body={})
    primary = Mock(side_effect=error)
    backup = Mock()
    with pytest.raises(AuthenticationError):
        com_fallback_de_limite(RunnableLambda(primary), RunnableLambda(backup)).invoke("x")
    backup.assert_not_called()


def test_middleware_troca_somente_o_passo_do_modelo():
    request = Mock()
    alternative = object()
    request.override.return_value = alternative
    handler = Mock(side_effect=[rate_limit(), "resposta"])
    middleware = GroqRateLimitFallback("groq")
    assert middleware.wrap_model_call(request, handler) == "resposta"
    request.override.assert_called_once_with(model="groq")
    assert handler.call_args_list[1].args == (alternative,)


async def test_middleware_async():
    request = Mock()
    handler = AsyncMock(side_effect=[rate_limit(), "resposta"])
    assert await GroqRateLimitFallback("groq").awrap_model_call(request, handler) == "resposta"
    assert handler.await_count == 2


def test_limite_dos_dois_provedores_e_propagado():
    with pytest.raises(RateLimitError):
        com_fallback_de_limite(RunnableLambda(Mock(side_effect=rate_limit())),
                              RunnableLambda(Mock(side_effect=rate_limit()))).invoke("x")
