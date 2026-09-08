import os
from psycopg2 import pool
from dotenv import load_dotenv

# Carrega as variáveis de ambiente
load_dotenv()

# Configurações do banco de dados
DB_CONFIG = {
    "host": os.getenv("POSTGRESQL_HOST"),
    "database": os.getenv("POSTGRESQL_DB"),
    "user": os.getenv("POSTGRESQL_USER"),
    "password": os.getenv("POSTGRESQL_PASSWORD"),
    "port": os.getenv("POSTGRESQL_PORT"),
}

# Criação do pool de conexões
connection_pool = pool.SimpleConnectionPool(
    minconn=1,  # Número mínimo de conexões no pool
    maxconn=10,  # Número máximo de conexões no pool
    **DB_CONFIG
)

def get_connection():
    """
    Obtém uma conexão do pool.
    """
    if connection_pool:
        return connection_pool.getconn()
    else:
        raise Exception("O pool de conexões não foi inicializado.")

def release_connection(conn):
    """
    Libera uma conexão de volta para o pool.
    """
    if connection_pool:
        connection_pool.putconn(conn)

def close_pool():
    """
    Fecha todas as conexões do pool.
    """
    if connection_pool:
        connection_pool.closeall()