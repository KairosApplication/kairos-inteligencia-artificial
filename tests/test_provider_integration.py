"""Exercita SDKs e LangChain reais com transporte HTTP simulado."""
import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

from data.model_fallback import GroqRateLimitFallback
from schemas import AvaliacaoGuardrail, AvaliacaoJuiz, ExtracaoMemoria


def completion(message, model):
    return {"id": "test-completion", "object": "chat.completion", "created": 0,
            "model": model, "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def test_limite_apos_ferramenta_nao_repete_consulta():
    primary_calls = []
    fallback_calls = []
    tool_calls = []

    def openai_transport(request):
        primary_calls.append(request)
        if len(primary_calls) == 1:
            return httpx.Response(200, json=completion({
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call-1", "type": "function",
                                "function": {"name": "consultar", "arguments": "{}"}}],
            }, "gpt-4.1-mini"))
        return httpx.Response(429, json={"error": {"message": "limit", "type": "rate_limit_exceeded", "code": "rate_limit_exceeded"}})

    def groq_transport(request):
        fallback_calls.append(request)
        assert b"Quantidade: 12" in request.content
        return httpx.Response(200, json=completion(
            {"role": "assistant", "content": "Há 12 unidades."}, "openai/gpt-oss-20b"))

    @tool
    def consultar() -> str:
        """Consulta estoque simulado."""
        tool_calls.append(1)
        return "Quantidade: 12"

    with httpx.Client(transport=httpx.MockTransport(openai_transport)) as primary_http, \
         httpx.Client(transport=httpx.MockTransport(groq_transport)) as backup_http:
        primary = ChatOpenAI(model="gpt-4.1-mini", api_key="test", max_retries=0, http_client=primary_http)
        backup = ChatGroq(model="openai/gpt-oss-20b", api_key="test", max_retries=0, http_client=backup_http)
        agent = create_agent(model=primary, tools=[consultar], middleware=[GroqRateLimitFallback(backup)])
        result = agent.invoke({"messages": [{"role": "user", "content": "Qual o estoque?"}]})
    assert result["messages"][-1].content == "Há 12 unidades."
    assert len(primary_calls) == 2
    assert len(fallback_calls) == 1
    assert len(tool_calls) == 1


@pytest.mark.parametrize("schema,payload", [
    (AvaliacaoGuardrail, '{"bloqueado":false,"categoria":"nenhuma","motivo":"legítima"}'),
    (AvaliacaoJuiz, '{"aprovado":true,"motivo":"fundamentada"}'),
    (ExtracaoMemoria, '{"memorias":[]}'),
])
def test_fallback_preserva_saida_estruturada(schema, payload):
    from llms import _estruturado

    def primary_handler(request):
        return httpx.Response(429, json={"error": {"message": "limit", "type": "rate_limit_exceeded"}})

    def fallback_handler(request):
        return httpx.Response(200, json=completion({"role": "assistant", "content": payload}, "openai/gpt-oss-20b"))

    with httpx.Client(transport=httpx.MockTransport(primary_handler)) as primary_http, \
         httpx.Client(transport=httpx.MockTransport(fallback_handler)) as backup_http:
        primary = ChatOpenAI(model="gpt-4.1-mini", api_key="test", max_retries=0, http_client=primary_http)
        backup = ChatGroq(model="openai/gpt-oss-20b", api_key="test", max_retries=0, http_client=backup_http)
        result = _estruturado(primary, backup, schema).invoke("pergunta")
    assert isinstance(result["parsed"], schema)
    assert result["parsing_error"] is None
