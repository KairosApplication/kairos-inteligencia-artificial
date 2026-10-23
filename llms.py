"""OpenAI como provedor principal e Groq apenas para limites de requisição."""
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

from config import settings
from data.model_fallback import GroqRateLimitFallback, com_fallback_de_limite
from schemas import AvaliacaoGuardrail, AvaliacaoJuiz, ExtracaoMemoria

# GPT-6 Luna exige reasoning_effort="none" para tools em Chat Completions.
llm_principal = ChatOpenAI(
    model="gpt-6-luna", temperature=0.6, max_retries=0,
    reasoning_effort="none", use_responses_api=False,
    timeout=settings.llm_timeout_seconds, max_completion_tokens=settings.llm_max_tokens,
)
llm_rapido = ChatOpenAI(
    model="gpt-6-luna", temperature=0, max_retries=0,
    reasoning_effort="none", use_responses_api=False,
    timeout=settings.llm_timeout_seconds, max_completion_tokens=settings.llm_max_tokens,
)

groq_principal = ChatGroq(
    model="openai/gpt-oss-120b", temperature=0.6, max_retries=0,
    timeout=settings.llm_timeout_seconds, max_tokens=settings.llm_max_tokens,
)
groq_rapido = ChatGroq(
    model="openai/gpt-oss-20b", temperature=0, max_retries=0,
    timeout=settings.llm_timeout_seconds, max_tokens=settings.llm_max_tokens,
)


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
