"""OpenAI como provedor principal e Groq apenas para limites de requisição."""
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

from config import settings
from data.model_fallback import GroqRateLimitFallback, com_fallback_de_limite
from schemas import AvaliacaoGuardrail, AvaliacaoJuiz, ExtracaoMemoria

# Modelos utilizados pelo projeto, visíveis para avaliação e versionados.
OPENAI_CHAT_MODEL = "gpt-4.1-mini"
OPENAI_FAST_MODEL = "gpt-4.1-mini"
GROQ_CHAT_MODEL = "openai/gpt-oss-120b"
GROQ_FAST_MODEL = "openai/gpt-oss-20b"
CHAT_TEMPERATURE = 0.2
FAST_TEMPERATURE = 0


def _openai(model, temperature):
    return ChatOpenAI(
        model=model, temperature=temperature, max_retries=0,
        timeout=settings.llm_timeout_seconds, max_tokens=settings.llm_max_tokens,
    )


def _groq(model, temperature):
    return ChatGroq(
        model=model, temperature=temperature, max_retries=0,
        timeout=settings.llm_timeout_seconds, max_tokens=settings.llm_max_tokens,
    )


llm_principal = _openai(OPENAI_CHAT_MODEL, CHAT_TEMPERATURE)
llm_rapido = _openai(OPENAI_FAST_MODEL, FAST_TEMPERATURE)
groq_principal = _groq(GROQ_CHAT_MODEL, CHAT_TEMPERATURE)
groq_rapido = _groq(GROQ_FAST_MODEL, FAST_TEMPERATURE)


def fallback_middleware(rapido=False):
    return GroqRateLimitFallback(groq_rapido if rapido else groq_principal)


def _estruturado(primary, fallback, schema):
    # Cada provedor faz seu próprio binding e parsing do mesmo schema.
    return com_fallback_de_limite(
        primary.with_structured_output(schema, method="json_schema", strict=True, include_raw=True),
        fallback.with_structured_output(schema, method="json_schema", strict=True, include_raw=True),
    )


guardrail = _estruturado(llm_rapido, groq_rapido, AvaliacaoGuardrail)
juiz = _estruturado(llm_principal, groq_principal, AvaliacaoJuiz)
extrator_memoria = _estruturado(llm_rapido, groq_rapido, ExtracaoMemoria)
llm_resumo = com_fallback_de_limite(llm_rapido, groq_rapido)
