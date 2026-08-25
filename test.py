from langchain_groq import ChatGroq
from langchain.agents import create_agent
from langgraph.graph import StateGraph
from langchain_google_genai import ChatGoogleGenerativeAI
from prompts import (ORQUESTRADOR_PROMPT, FAQ_PROMPT, JUIZ_PROMPT)
from tools.faq import FAQ_TOOLS
from dotenv import load_dotenv
import os

load_dotenv()

# LLMs
llm_groq = ChatGroq(
    model="gpt/oss-120b",
    temperature=0.7,
    api_key=os.getenv("GROQ_API_KEY")
)

llm_rapido = ChatGroq(
    model="gpt/oss-20b",
    temperature=0.0,
    api_key=os.getenv("GROQ_API_KEY")
)

agente_faq = create_agent(
    model=llm_rapido,
    tools=FAQ_TOOLS,
    system_prompt=FAQ_PROMPT
)

session_id = 45

pergunta = input()
resposta = agente_faq.invoke(
    {"messages": [{"role": "human", "content": pergunta}]},
    config={"configurable": {"thread_id": session_id}}
)

print(resposta["messages"][-1].text)