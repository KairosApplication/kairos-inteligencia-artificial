from langchain.agents import create_agent

from data.conversation_middleware import MemoryContext
from llms import llm_groq, llm_rapido, resumo_middleware
from prompts import CLIENTES_PROMPT, ESTOQUE_PROMPT, FAQ_PROMPT, ORQUESTRADOR_PROMPT, VENDAS_PROMPT
from schemas import Roteamento
from tools.clientes import CLIENTES_TOOLS
from tools.estoque import ESTOQUE_TOOLS
from tools.faq import FAQ_TOOLS
from tools.vendas import VENDAS_TOOLS

# AGENTE ORQUESTRADOR
# Não recebe tools: sua única função é classificar a intenção do
# usuário e retornar a rota estruturada (ver schemas.Roteamento) via
# `response_format`. O roteamento em si é feito pelo LangGraph
# (ver graph_nodes.decidir_rota), não pelo agente.
agente_orquestrador = create_agent(
    model=llm_rapido,
    tools=[],
    system_prompt=ORQUESTRADOR_PROMPT,
    response_format=Roteamento
)

# AGENTE FAQ
agente_faq = create_agent(
    model=llm_rapido,
    tools=FAQ_TOOLS,
    system_prompt=FAQ_PROMPT,
    middleware=[resumo_middleware],
    context_schema=MemoryContext
)

# AGENTE ESTOQUE
agente_estoque = create_agent(
    model=llm_groq,
    tools=ESTOQUE_TOOLS,
    system_prompt=ESTOQUE_PROMPT,
    middleware=[resumo_middleware],
    context_schema=MemoryContext
)

# AGENTE VENDAS
agente_vendas = create_agent(
    model=llm_groq,
    tools=VENDAS_TOOLS,
    system_prompt=VENDAS_PROMPT,
    middleware=[resumo_middleware],
    context_schema=MemoryContext
)

# AGENTE CLIENTES
agente_clientes = create_agent(
    model=llm_groq,
    tools=CLIENTES_TOOLS,
    system_prompt=CLIENTES_PROMPT,
    middleware=[resumo_middleware],
    context_schema=MemoryContext
)
