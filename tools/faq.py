"""FAQ com embeddings OpenAI e cache local sem desserialização de pickle."""
import hashlib
import json
import sqlite3
from functools import lru_cache

from langchain.tools import tool
from langchain_community.document_loaders import PyPDFLoader
from langchain_community.vectorstores import FAISS
from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from config import settings


class CachedEmbeddings(Embeddings):
    """Cache por texto e modelo; armazena somente vetores JSON em SQLite."""
    def __init__(self, delegate, path, namespace):
        self.delegate = delegate
        self.path = path
        self.namespace = namespace
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, vector TEXT NOT NULL)")

    def _key(self, text):
        return hashlib.sha256(json.dumps([self.namespace, text]).encode()).hexdigest()

    def embed_documents(self, texts):
        with sqlite3.connect(self.path) as db:
            cached = [db.execute("SELECT vector FROM embeddings WHERE key = ?", (self._key(t),)).fetchone() for t in texts]
            missing = [i for i, row in enumerate(cached) if row is None]
            if missing:
                vectors = self.delegate.embed_documents([texts[i] for i in missing])
                if len(vectors) != len(missing):
                    raise ValueError("Quantidade inesperada de embeddings retornados.")
                for i, vector in zip(missing, vectors):
                    serialized = json.dumps(vector)
                    db.execute("INSERT OR REPLACE INTO embeddings VALUES (?, ?)", (self._key(texts[i]), serialized))
                    cached[i] = (serialized,)
            return [json.loads(row[0]) for row in cached]

    def embed_query(self, text):
        return self.embed_documents([text])[0]


@lru_cache(maxsize=1)
def create_faq_database():
    docs = PyPDFLoader(str(settings.faq_pdf_path)).load()
    chunks = RecursiveCharacterTextSplitter(
        chunk_size=settings.faq_chunk_size, chunk_overlap=settings.faq_chunk_overlap,
    ).split_documents(docs)
    embeddings = CachedEmbeddings(
        OpenAIEmbeddings(model=settings.openai_embedding_model, max_retries=2,
                         request_timeout=settings.llm_timeout_seconds),
        settings.faq_cache_path, settings.openai_embedding_model,
    )
    return FAISS.from_documents(chunks, embeddings)


@tool
def faq_retriever(question: str) -> str:
    """Busca trechos relevantes da documentação Kairos com sua página de origem."""
    results = create_faq_database().max_marginal_relevance_search(
        question, k=settings.faq_k, fetch_k=settings.faq_fetch_k,
        lambda_mult=settings.faq_lambda_mult,
    )
    return "\n\n".join(
        f"[Fonte: FAQ Kairos, página {result.metadata.get('page', 0) + 1}]\n{result.page_content}"
        for result in results
    )


FAQ_TOOLS = [faq_retriever]
