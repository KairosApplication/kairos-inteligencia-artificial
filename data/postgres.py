"""Pool PostgreSQL de leitura, inicializado sob demanda e seguro entre threads."""
import os
import threading

from psycopg2 import pool

from config import settings

connection_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global connection_pool
    with _pool_lock:
        if connection_pool is None:
            connection_pool = pool.ThreadedConnectionPool(
                minconn=settings.db_pool_min, maxconn=settings.db_pool_max,
                host=os.getenv("POSTGRESQL_HOST"), database=os.getenv("POSTGRESQL_DB"),
                user=os.getenv("POSTGRESQL_USER"), password=os.getenv("POSTGRESQL_PASSWORD"),
                port=os.getenv("POSTGRESQL_PORT") or "5432",
                connect_timeout=settings.db_timeout_seconds,
                options=f"-c statement_timeout={settings.db_statement_timeout_ms}",
            )
        return connection_pool


def get_connection():
    conn = _get_pool().getconn()
    try:
        conn.set_session(readonly=True, autocommit=False)
    except Exception:
        release_connection(conn)
        raise
    return conn


def release_connection(conn):
    if connection_pool is not None:
        connection_pool.putconn(conn)


def executar_consulta(sql: str, params: dict) -> list:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    finally:
        release_connection(conn)


def close_pool():
    global connection_pool
    with _pool_lock:
        if connection_pool is not None:
            connection_pool.closeall()
            connection_pool = None
