"""Testes offline: nenhuma credencial real nem conexão de rede."""
import os
import socket

import pytest

os.environ["OPENAI_API_KEY"] = "test-openai-not-a-real-key"
os.environ["GROQ_API_KEY"] = "test-groq-not-a-real-key"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGSMITH_API_KEY"] = ""


@pytest.fixture(autouse=True)
def bloquear_rede(monkeypatch):
    original_connect = socket.socket.connect
    def fail(*args, **kwargs):
        raise AssertionError("Teste tentou acessar a rede.")
    def guarded_connect(sock, address):
        # O event loop do Windows usa um socketpair de loopback internamente.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        return fail()
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", fail)
