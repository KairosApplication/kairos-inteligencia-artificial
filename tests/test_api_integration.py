from unittest.mock import Mock

import httpx
import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from pydantic import SecretStr

import api
from config import settings
from data.ai_api import AiApiClient, PersistenceError, decode, encode
from data import memory_store, api_observability
from data.request_context import chat_context

ID = "000000000000000000000001"
TOKEN = "internal-test-token-with-at-least-32-characters"


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setattr(settings, "chatbot_service_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "persistence_backend", "api")
    return TestClient(api.app)


def test_servico_sem_token_nao_acessa_grafo(app_client, monkeypatch):
    graph = Mock()
    monkeypatch.setattr(api, "execute_graph", graph)
    assert app_client.post("/api/v1/chat/messages", json={"userId": 1, "question": "Oi"}).status_code == 401
    graph.assert_not_called()


def test_usuario_nao_pode_continuar_conversa_de_outro(app_client, monkeypatch):
    persistence = Mock()
    persistence.conversation.side_effect = PersistenceError(404)
    graph = Mock()
    monkeypatch.setattr(api, "get_ai_api", lambda: persistence)
    monkeypatch.setattr(api, "execute_graph", graph)
    result = app_client.post("/api/v1/chat/messages", headers={"Authorization": "Bearer " + TOKEN},
                             json={"userId": 2, "conversationId": ID, "question": "Oi"})
    assert result.status_code == 404
    persistence.conversation.assert_called_once_with(2, ID)
    graph.assert_not_called()


def test_concorrencia_na_mesma_conversa_nao_executa_modelo(app_client, monkeypatch):
    persistence = Mock()
    persistence.active_conversation.return_value = {"_id": ObjectId(ID), "user_id": 1}
    persistence.lease.side_effect = PersistenceError(409)
    graph = Mock()
    monkeypatch.setattr(api, "get_ai_api", lambda: persistence)
    monkeypatch.setattr(api, "execute_graph", graph)
    response = app_client.post("/api/v1/chat/messages", headers={"Authorization": "Bearer " + TOKEN},
                               json={"userId": 1, "question": "Oi"})
    assert response.status_code == 409
    graph.assert_not_called()


def test_falha_no_grafo_libera_reserva_e_contexto(app_client, monkeypatch):
    persistence = Mock()
    persistence.active_conversation.return_value = {"_id": ObjectId(ID), "user_id": 1}
    monkeypatch.setattr(api, "get_ai_api", lambda: persistence)
    monkeypatch.setattr(api, "execute_graph", Mock(side_effect=RuntimeError("secret-token")))
    response = app_client.post("/api/v1/chat/messages", headers={"Authorization": "Bearer " + TOKEN},
                               json={"userId": 1, "question": "Oi"})
    assert response.status_code == 503
    assert "secret-token" not in response.text
    assert persistence.lease.call_args.kwargs == {"action": "release"}
    assert chat_context.get() is None


def test_resposta_preserva_conversa_e_limpa_contexto(app_client, monkeypatch):
    persistence = Mock()
    persistence.conversation.return_value = {"_id": ObjectId(ID), "user_id": 1}
    monkeypatch.setattr(api, "get_ai_api", lambda: persistence)

    def graph(state):
        assert state["conversation_id"] == ObjectId(ID)
        assert chat_context.get().user_id == 1
        return {"resposta": "Qual período?", "execution_id": ObjectId(ID)}

    monkeypatch.setattr(api, "execute_graph", graph)
    response = app_client.post("/api/v1/chat/messages", headers={"Authorization": "Bearer " + TOKEN},
                               json={"userId": 1, "conversationId": ID, "question": "Vendas"})
    assert response.status_code == 200
    assert response.json()["conversationId"] == ID
    assert response.json()["answer"] == "Qual período?"
    persistence.active_conversation.assert_not_called()


def test_lista_de_conversas_filtra_usuario_e_nao_expoe_mensagens(app_client, monkeypatch):
    persistence = Mock()
    persistence.request.return_value = {"items": [{"id": ID, "status": "ACTIVE", "messages": ["private"]}],
                                        "page": 0, "size": 20, "hasNext": False}
    monkeypatch.setattr(api, "get_ai_api", lambda: persistence)
    response = app_client.get("/api/v1/chat/users/7/conversations", headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 200
    assert "messages" not in response.json()["items"][0]
    assert persistence.request.call_args.kwargs["params"]["userId"] == 7


def test_criar_conversa_nao_reutiliza_a_ativa(app_client, monkeypatch):
    persistence = Mock()
    persistence.create.return_value = {"_id": ObjectId(ID), "user_id": 7, "status": "ACTIVE"}
    monkeypatch.setattr(api, "get_ai_api", lambda: persistence)
    response = app_client.post("/api/v1/chat/conversations", headers={"Authorization": "Bearer " + TOKEN}, json={"userId": 7})
    assert response.status_code == 201
    persistence.create.assert_called_once_with("conversations", {"user_id": 7, "status": "ACTIVE", "summarized_count": 0})


def test_contrato_http_preserva_dados_dinamicos_e_datas():
    calls = []

    def handle(request):
        import json
        calls.append((request.url.path, json.loads(request.content)))
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        return httpx.Response(201, json={"id": ID, **calls[-1][1]})

    client = AiApiClient("http://127.0.0.1:8081", TOKEN, transport=httpx.MockTransport(handle))
    result = client.create("agent-traces", {"execution_id": ObjectId(ID), "started_at": "2026-10-18 12:00:00",
                                           "input": {"campo_do_negocio": "preservado"}})
    assert calls[0][0] == "/api/v1/agent-traces"
    assert calls[0][1]["startedAt"] == "2026-10-18T12:00:00+00:00"
    assert calls[0][1]["input"] == {"campo_do_negocio": "preservado"}
    assert result["execution_id"] == ObjectId(ID)


def test_cas_desatualizado_nao_reenvia_resumo():
    transport = Mock(return_value=httpx.Response(409))
    client = AiApiClient("http://localhost:8081", TOKEN, transport=httpx.MockTransport(transport))
    assert client.summarize(1, ID, "novo", 0, 4, None) is False
    assert transport.call_count == 1


@pytest.mark.parametrize("status", [401, 403, 500])
def test_cliente_nao_vaza_resposta_remota_ou_token(status):
    client = AiApiClient("http://localhost:8081", TOKEN, transport=httpx.MockTransport(
        lambda _: httpx.Response(status, text="password=secret")))
    with pytest.raises(PersistenceError) as caught:
        client.get("conversations", ID)
    assert "secret" not in str(caught.value)
    assert caught.value.status == status


def test_memoria_http_envia_turno_para_endpoint_atomico(monkeypatch):
    persistence = Mock()
    persistence.get.return_value = {"_id": ObjectId(ID), "user_id": 7}
    monkeypatch.setattr(settings, "persistence_backend", "api")
    monkeypatch.setattr(memory_store, "get_ai_api", lambda: persistence)
    memory_store.registrar_turno(ObjectId(ID), "pergunta", "resposta")
    assert persistence.append_turn.call_args.args[:4] == (7, ObjectId(ID), "pergunta", "resposta")
    persistence.patch.assert_not_called()


def test_juiz_nao_inventa_booleanos_desconhecidos(monkeypatch):
    persistence = Mock()
    monkeypatch.setattr(api_observability, "get_ai_api", lambda: persistence)
    api_observability.registrar_avaliacao_juiz(ObjectId(ID), False, "Sem evidência")
    fields = persistence.create.call_args.args[1]
    assert fields["grounded"] is None
    assert fields["hallucination_detected"] is None


def test_custo_incompleto_continua_desconhecido(monkeypatch):
    persistence = Mock()
    persistence.list.return_value = iter([
        {"langsmith_run_id": "a", "estimated_cost_usd": .01},
        {"langsmith_run_id": "b", "estimated_cost_usd": None},
    ])
    monkeypatch.setattr(api_observability, "get_ai_api", lambda: persistence)
    api_observability._atualizar_custo(ObjectId(ID))
    assert persistence.patch.call_args.args[2] == {
        "estimated_cost_usd": None, "known_cost_usd": .01, "cost_complete": False,
    }


def test_api_interna_persiste_turno_pelo_contrato_java(app_client, monkeypatch):
    import json

    calls = []

    def java(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, path, body, request.headers.get("X-Chat-Lease")))
        if path.endswith("/lease") or path.endswith("/release"):
            return httpx.Response(204)
        if path.endswith("/turns"):
            return httpx.Response(200, json={"id": ID})
        return httpx.Response(200, json={"id": ID, "userId": 7, "status": "ACTIVE", "messages": []})

    persistence = AiApiClient("http://localhost:8081", TOKEN, transport=httpx.MockTransport(java))
    monkeypatch.setattr(api, "get_ai_api", lambda: persistence)
    monkeypatch.setattr(memory_store, "get_ai_api", lambda: persistence)

    def graph(state):
        memory_store.registrar_turno(state["conversation_id"], state["pergunta"], "Resposta verificada")
        return {"resposta": "Resposta verificada", "execution_id": ObjectId(ID), "aprovado": True}

    monkeypatch.setattr(api, "execute_graph", graph)
    response = app_client.post("/api/v1/chat/messages", headers={"Authorization": "Bearer " + TOKEN},
                               json={"userId": 7, "conversationId": ID, "question": "Estoque?"})
    assert response.status_code == 200
    turn = next(call for call in calls if call[1].endswith("/turns"))
    assert turn[2]["question"] == "Estoque?"
    assert turn[2]["answer"] == "Resposta verificada"
    assert turn[2]["turnId"] == turn[3]
    assert not any(method == "PATCH" and path.endswith(ID) for method, path, _, _ in calls)
