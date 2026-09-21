import os

import certifi
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.database import Database

# Carrega as variáveis de ambiente
load_dotenv()

NOME_BANCO = "kairos_ai"

# Cliente único, criado sob demanda (lazy) e reutilizado entre chamadas.
_client: MongoClient | None = None


def get_client() -> MongoClient:
    """
    Obtém o cliente MongoDB, criando-o na primeira chamada.

    `tlsCAFile=certifi.where()` garante que o handshake TLS utilize a
    cadeia de certificados do certifi em vez de depender do armazenamento
    de certificados do sistema operacional, que em alguns ambientes
    Windows está desatualizado e causa falha de conexão com o Atlas.
    """
    global _client

    if _client is None:
        uri = os.getenv("MONGODB_URI")

        if not uri:
            raise RuntimeError("MONGODB_URI não foi configurada no .env.")

        _client = MongoClient(uri, tlsCAFile=certifi.where())

    return _client


def get_database() -> Database:
    """
    Obtém o banco de dados do Kairos no cluster MongoDB.
    """
    return get_client()[NOME_BANCO]


def close_client() -> None:
    """
    Fecha o cliente MongoDB, caso tenha sido criado.
    """
    global _client

    if _client is not None:
        _client.close()
        _client = None
