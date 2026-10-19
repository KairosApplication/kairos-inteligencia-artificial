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
    def fail(*args, **kwargs):
        raise AssertionError("Teste tentou acessar a rede.")
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
