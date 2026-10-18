import os

from langchain_groq import ChatGroq

from data.conversation_middleware import MongoSummarizationMiddleware
from schemas import AvaliacaoGuardrail, AvaliacaoJuiz, ExtracaoMemoria

# LLMs
llm_groq = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.7,
    api_key=os.getenv("GROQ_API_KEY")
)

llm_rapido = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0.0,
    api_key=os.getenv("GROQ_API_KEY")
)

# GUARDRAIL DE SEGURANÇA
# Executado antes do Orquestrador. Não é um agente com tools.
# Ele apenas avalia a mensagem do usuário e retorna uma decisão
# estruturada sobre bloqueio.
#
# `include_raw=True` faz com que a chamada devolva um dict com as
# chaves "raw" (AIMessage bruta) e "parsed" (instância validada do
# schema), em vez de apenas o schema já validado. Os nós em
# `graph_nodes.py` usam apenas "parsed"; tokens, custo e latência de
# cada chamada são obtidos do LangSmith (ver
# `data/langsmith_client.py`), não mais da AIMessage bruta.
guardrail = llm_rapido.with_structured_output(
    AvaliacaoGuardrail,
    method="json_schema",
    strict=True,
    include_raw=True
)

# JUIZ
# Assim como o Orquestrador, o Juiz não precisa de tools.
# Ele apenas avalia a resposta e retorna uma decisão estruturada.
juiz = llm_groq.with_structured_output(
    AvaliacaoJuiz,
    method="json_schema",
    strict=True,
    include_raw=True
)

# AGENTE DE MEMÓRIA DE LONGO PRAZO
# Executado depois do Juiz, somente quando a resposta é aprovada.
# Extrai fatos/preferências duradouros sobre o usuário para a
# collection `user_memory`, reaproveitados como contexto adicional
# em turnos futuros.
extrator_memoria = llm_rapido.with_structured_output(
    ExtracaoMemoria,
    method="json_schema",
    strict=True,
    include_raw=True
)

# MIDDLEWARE DE SUMARIZAÇÃO COM MEMÓRIA DE LONGO PRAZO
#
# Cada agente especialista recebe sua própria instância do
# middleware. Diferente do uso convencional do SummarizationMiddleware
# (que só resume depois de um grande volume acumulado de mensagens ou
# tokens), aqui a sumarização é incremental: ocorre a cada turno em
# que já existir pelo menos uma mensagem anterior no contexto
# carregado do MongoDB, incorporando a interação recém-concluída ao
# resumo. `keep=("messages", 1)` mantém de fora do resumo somente a
# mensagem mais recente (a pergunta atual do usuário, que ainda não
# tem resposta no momento em que o middleware roda); todo o restante
# do histórico carregado é incorporado ao resumo acumulado.
#
# `trigger=("messages", 2)` dispara a sumarização a partir do momento
# em que há pelo menos 2 mensagens no contexto do turno (ex.: um
# resumo anterior + a pergunta atual, ou uma mensagem de histórico +
# a pergunta atual) — ou seja, em praticamente todo turno que não seja
# o primeiro de uma conversa.
#
# O `MongoSummarizationMiddleware` (ver data/conversation_middleware.py)
# estende esse comportamento padrão do LangChain para também persistir
# o resumo gerado na conversa do usuário no MongoDB, via o
# `MemoryContext` fornecido em `context` no momento do `invoke`.
resumo_middleware = MongoSummarizationMiddleware(
    model=llm_rapido,
    trigger=("messages", 2),
    keep=("messages", 1)
)
