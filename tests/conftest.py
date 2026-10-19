"""Testes offline: nenhuma credencial real nem conexão de rede."""
import os
import socket
import sys

import pytest

os.environ["OPENAI_API_KEY"] = "test-openai-not-a-real-key"
os.environ["GROQ_API_KEY"] = "test-groq-not-a-real-key"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGSMITH_API_KEY"] = ""
_socket_connect = socket.socket.connect


@pytest.fixture(autouse=True)
def bloquear_rede(monkeypatch):
    def fail(*args, **kwargs):
        # No Windows, o event loop do TestClient cria um par local de sockets.
        # Só essa chamada interna da biblioteca padrão é permitida.
        caller = sys._getframe(1)
        if caller.f_code.co_name == "_fallback_socketpair" and caller.f_code.co_filename == socket.__file__:
            return _socket_connect(*args, **kwargs)
        raise AssertionError("Teste tentou acessar a rede.")
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
