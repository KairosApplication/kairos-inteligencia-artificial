import os

import certifi
from pymongo import MongoClient
from pymongo.database import Database
from app.config import settings

# Cliente único, criado sob demanda (lazy) e reutilizado entre chamadas.
_client: MongoClient | None = None


def get_client() -> MongoClient:
    """Reutiliza o cliente MongoDB com certificados do certifi para validar TLS."""
    global _client

    if _client is None:
        uri = os.getenv("MONGODB_URI")

        if not uri:
            raise RuntimeError("MONGODB_URI não foi configurada no .env.")

        tls_options = {"tlsCAFile": certifi.where()} if uri.startswith("mongodb+srv://") or "tls=true" in uri.lower() else {}
        _client = MongoClient(
            uri, **tls_options,
            serverSelectionTimeoutMS=settings.db_timeout_seconds * 1000,
            connectTimeoutMS=settings.db_timeout_seconds * 1000,
            socketTimeoutMS=settings.db_timeout_seconds * 1000,
        )

    return _client


def get_database() -> Database:
    """
    Obtém o banco de dados do Kairos no cluster MongoDB.
    """
    nome_banco = (settings.mongodb_database or "").strip()
    if not nome_banco:
        raise RuntimeError("MONGODB_DATABASE não foi configurada no ambiente.")
    return get_client()[nome_banco]


def close_client() -> None:
    """
    Fecha o cliente MongoDB, caso tenha sido criado.
    """
    global _client

    if _client is not None:
        _client.close()
        _client = None
