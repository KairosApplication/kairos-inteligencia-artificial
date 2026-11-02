"""FAQ com embeddings OpenAI e cache persistente em SQLite."""
import hashlib
import json
import sqlite3
from functools import lru_cache
from threading import Lock

from langchain.tools import tool
from langchain_community.document_loaders import PyPDFLoader
from langchain_community.vectorstores import FAISS
from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import settings
from app.answer_sources import consulted

EMBEDDING_MODEL = "text-embedding-3-small"
_cache_lock = Lock()


class CachedEmbeddings(Embeddings):
    """Cache por texto e modelo; armazena somente vetores JSON em SQLite."""
    def __init__(self, delegate, path, namespace):
        self.delegate = delegate
        self.path = path
        self.namespace = namespace
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, vector TEXT NOT NULL)")

    def _key(self, text, kind="document"):
        return hashlib.sha256(json.dumps([self.namespace, kind, text]).encode()).hexdigest()

    def embed_documents(self, texts):
        with _cache_lock, sqlite3.connect(self.path) as db:
            cached = {text: db.execute("SELECT vector FROM embeddings WHERE key = ?", (self._key(text),)).fetchone()
                      for text in dict.fromkeys(texts)}
            missing = [text for text, row in cached.items() if row is None]
            if missing:
                vectors = self.delegate.embed_documents(missing)
                if len(vectors) != len(missing):
                    raise ValueError("Quantidade inesperada de embeddings retornados.")
                for text, vector in zip(missing, vectors):
                    serialized = json.dumps(vector)
                    db.execute("INSERT OR REPLACE INTO embeddings VALUES (?, ?)", (self._key(text), serialized))
                    cached[text] = (serialized,)
            return [json.loads(cached[text][0]) for text in texts]

    def embed_query(self, text):
        # Mantém os espaços de consulta e documento separados no cache.
        key = self._key(text, "query")
        with _cache_lock, sqlite3.connect(self.path) as db:
            row = db.execute("SELECT vector FROM embeddings WHERE key = ?", (key,)).fetchone()
            if row is not None:
                return json.loads(row[0])
            vector = self.delegate.embed_query(text)
            db.execute("INSERT OR REPLACE INTO embeddings VALUES (?, ?)", (key, json.dumps(vector)))
            return vector


@lru_cache(maxsize=1)
def create_faq_database():
    docs = PyPDFLoader(str(settings.faq_pdf_path)).load()
    chunks = RecursiveCharacterTextSplitter(
        chunk_size=settings.faq_chunk_size, chunk_overlap=settings.faq_chunk_overlap,
    ).split_documents(docs)
    embeddings = CachedEmbeddings(
        OpenAIEmbeddings(
            model=EMBEDDING_MODEL, dimensions=1536, max_retries=0,
            request_timeout=settings.llm_timeout_seconds,
            chunk_size=settings.faq_embedding_batch_size,
        ),
        settings.faq_cache_path, f"openai:{EMBEDDING_MODEL}:1536",
    )
    return FAISS.from_documents(chunks, embeddings)


@tool
def faq_retriever(question: str) -> str:
    """Busca trechos relevantes da documentação Kairos com sua página de origem."""
    results = create_faq_database().max_marginal_relevance_search(
        question, k=settings.faq_k, fetch_k=settings.faq_fetch_k,
        lambda_mult=settings.faq_lambda_mult,
    )
    if results:
        consulted("pdf")
    return "\n\n".join(
        f"[Fonte: FAQ Kairos, página {result.metadata.get('page', 0) + 1}]\n{result.page_content}"
        for result in results
    )


FAQ_TOOLS = [faq_retriever]
