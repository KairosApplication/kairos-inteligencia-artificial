import os
from dotenv import load_dotenv

from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain.tools import tool

load_dotenv()

PDF_PATH = "data/FAQ_Kairos.pdf"

# Criação do banco vetorial
def create_faq_database():
    """
    Realiza a criação do banco vetorial para armazenar o documento FAQ.
    Por último, retorna o banco vetorial criado.
    """
    loader = PyPDFLoader(PDF_PATH)
    docs = loader.load()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=600,
        chunk_overlap=50
    )
    
    chunks = splitter.split_documents(docs)
    embeddings = GoogleGenerativeAIEmbeddings(
        model="gemini-embedding-2-preview",
        google_api_key=os.getenv("GOOGLE_API_KEY")
    )
    
    db = FAISS.from_documents(
        chunks,
        embeddings
    )
    return db

faq_db = create_faq_database()

@tool
def faq_retriever(question: str) -> str:
    """
    Busca na base de conhecimento do FAQ do Kairos
    os trechos mais relevantes para responder à pergunta.
    """
    results = faq_db.max_marginal_relevance_search(
        question,
        k=4,
        fetch_k=10,
        lambda_mult=0.7
    )

    textos = [result.page_content for result in results]
    
    return "\n\n".join(textos)

FAQ_TOOLS = [create_faq_database, faq_retriever]