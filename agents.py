from langchain.agents import create_agent

from data.conversation_middleware import MemoryContext
from llms import fallback_middleware, llm_principal, llm_rapido
from prompts import CLIENTES_PROMPT, ESTOQUE_PROMPT, FAQ_PROMPT, ORQUESTRADOR_PROMPT, VENDAS_PROMPT
from schemas import Roteamento
from tools.clientes import CLIENTES_TOOLS
from tools.estoque import ESTOQUE_TOOLS
from tools.faq import FAQ_TOOLS
from tools.vendas import VENDAS_TOOLS

# Classifica a intenção sem tools; o LangGraph aplica a rota estruturada.
agente_orquestrador = create_agent(
    model=llm_rapido,
    tools=[],
    system_prompt=ORQUESTRADOR_PROMPT,
    response_format=Roteamento,
    middleware=[fallback_middleware(rapido=True)]
)

agente_faq = create_agent(
    model=llm_rapido,
    tools=FAQ_TOOLS,
    system_prompt=FAQ_PROMPT,
    middleware=[fallback_middleware(rapido=True)],
    context_schema=MemoryContext
)

agente_estoque = create_agent(
    model=llm_principal,
    tools=ESTOQUE_TOOLS,
    system_prompt=ESTOQUE_PROMPT,
    middleware=[fallback_middleware()],
    context_schema=MemoryContext
)

agente_vendas = create_agent(
    model=llm_principal,
    tools=VENDAS_TOOLS,
    system_prompt=VENDAS_PROMPT,
    middleware=[fallback_middleware()],
    context_schema=MemoryContext
)

agente_clientes = create_agent(
    model=llm_principal,
    tools=CLIENTES_TOOLS,
    system_prompt=CLIENTES_PROMPT,
    middleware=[fallback_middleware()],
    context_schema=MemoryContext
)
