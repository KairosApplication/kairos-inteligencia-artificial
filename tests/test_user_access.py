"""Fronteiras de autenticação, isolamento de conversas e consultas por perfil."""
from contextlib import contextmanager
from unittest.mock import Mock

import httpx
import pytest
from bson import ObjectId
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import api
from config import settings
from data import auth, api_observability, memory_store
from data.access import Principal, require_domain
from data.request_context import ChatContext, chat_context
from tools import clientes, estoque, vendas

ID = "000000000000000000000001"
MANAGER = Principal(7, "MANAGER")
STOCKER = Principal(7, "STOCKER", sector_ids=(2, 3))
CUSTOMER = Principal(7, "CUSTOMER", customer_id=11)


@contextmanager
def identity_context(identity):
    token = chat_context.set(ChatContext(identity.user_id, ObjectId(ID), "test-request", identity))
    try:
        yield
    finally:
        chat_context.reset(token)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "persistence_backend", "api")
    persistence = Mock()
    monkeypatch.setattr(api, "get_ai_api", lambda: persistence)
    api.app.dependency_overrides[auth.authenticated_user] = lambda: CUSTOMER
    try:
        yield TestClient(api.app), persistence
    finally:
        api.app.dependency_overrides.clear()


def test_identity_cannot_be_selected_in_message(client):
    browser, persistence = client
    result = browser.post("/api/v1/me/chat/messages", json={"question": "Oi", "userId": 99, "role": "MANAGER"})
    assert result.status_code == 422
    persistence.conversation.assert_not_called()


@pytest.mark.parametrize("document", [
    {"user_id": 99, "access_scope": CUSTOMER.scope},
    {"user_id": 7, "access_scope": MANAGER.scope, "summary": "gerencial"},
    {"user_id": 7, "summary": "legado sem escopo"},
])
def test_cannot_read_or_continue_foreign_or_previous_profile_history(client, monkeypatch, document):
    browser, persistence = client
    persistence.conversation.return_value = {"_id": ObjectId(ID), **document}
    graph = Mock()
    monkeypatch.setattr(api, "execute_graph", graph)
    read = browser.get(f"/api/v1/me/chat/conversations/{ID}")
    send = browser.post("/api/v1/me/chat/messages", json={"question": "Oi", "conversationId": ID})
    assert read.status_code == send.status_code == 404
    assert "gerencial" not in read.text
    graph.assert_not_called()
    persistence.lease.assert_not_called()


def test_message_passes_server_identity_and_releases_context(client, monkeypatch):
    browser, persistence = client
    persistence.conversation.return_value = {"_id": ObjectId(ID), "user_id": 7, "access_scope": CUSTOMER.scope}

    def graph(state):
        assert state["user_id"] == 7
        assert chat_context.get().principal == CUSTOMER
        with pytest.raises(PermissionError):
            require_domain("vendas")
        return {"resposta": "Olá", "execution_id": ObjectId(ID)}

    monkeypatch.setattr(api, "execute_graph", graph)
    result = browser.post("/api/v1/me/chat/messages", json={"question": "Oi", "conversationId": ID})
    assert result.status_code == 200
    assert chat_context.get() is None
    assert persistence.lease.call_args.kwargs == {"action": "release"}


def test_new_and_active_conversations_use_current_scope(client, monkeypatch):
    browser, persistence = client
    persistence.request.return_value = {"items": []}
    persistence.create.return_value = {"_id": ObjectId(ID), "user_id": 7, "access_scope": CUSTOMER.scope, "status": "ACTIVE"}
    monkeypatch.setattr(api, "execute_graph", lambda _: {"resposta": "Oi", "execution_id": ObjectId(ID)})
    assert browser.post("/api/v1/me/chat/messages", json={"question": "Oi"}).status_code == 200
    assert persistence.request.call_args.kwargs["params"]["accessScope"] == CUSTOMER.scope
    assert persistence.create.call_args.args[1]["access_scope"] == CUSTOMER.scope
    persistence.active_conversation.assert_not_called()


def test_list_does_not_expose_history_from_other_scope(client):
    browser, persistence = client
    persistence.request.return_value = {"items": [
        {"id": ID, "userId": 7, "accessScope": CUSTOMER.scope, "messages": ["private"]},
        {"id": "other", "userId": 7, "accessScope": MANAGER.scope},
    ], "hasNext": False}
    result = browser.get("/api/v1/me/chat/conversations")
    assert result.status_code == 200
    assert [item["id"] for item in result.json()["items"]] == [ID]
    assert "messages" not in result.text


@pytest.mark.parametrize("tool", estoque.ESTOQUE_TOOLS)
def test_stock_queries_always_apply_numeric_sector_scope(monkeypatch, tool):
    query = Mock(return_value=[])
    monkeypatch.setattr(estoque, "executar_consulta", query)
    with identity_context(STOCKER):
        tool.invoke({"setor": "outro setor"})
    sql, params = query.call_args.args
    assert "sec.id = ANY(%(allowed_sectors)s::bigint[])" in sql
    assert params["allowed_sectors"] == [2, 3]
    with identity_context(MANAGER):
        tool.invoke({})
    assert query.call_args.args[1]["allowed_sectors"] is None


@pytest.mark.parametrize("identity", [CUSTOMER, Principal(7, "STOCKER")])
def test_customer_and_stocker_without_sector_do_not_query_stock(monkeypatch, identity):
    query = Mock()
    monkeypatch.setattr(estoque, "executar_consulta", query)
    with identity_context(identity):
        estoque.consultar_niveis_estoque.invoke({})
    query.assert_not_called()


@pytest.mark.parametrize("tool,args", [
    (vendas.consultar_historico_vendas, {"dias": 7}),
    (vendas.analisar_tendencia_vendas, {"semanas": 4}),
    (vendas.prever_demanda_futura, {"semanas_historico": 4, "semanas_previsao": 2}),
])
@pytest.mark.parametrize("identity", [CUSTOMER, STOCKER])
def test_sales_tools_deny_non_managers_before_query(monkeypatch, tool, args, identity):
    query = Mock()
    monkeypatch.setattr(vendas, "executar_consulta", query)
    with identity_context(identity):
        tool.invoke(args)
    query.assert_not_called()


@pytest.mark.parametrize("tool", [clientes.consultar_historico_compras_cliente, clientes.sugerir_ofertas_personalizadas])
def test_customer_cannot_query_someone_else_even_through_direct_tool(monkeypatch, tool):
    query = Mock(return_value=[])
    monkeypatch.setattr(clientes, "executar_consulta", query)
    with identity_context(CUSTOMER):
        tool.invoke({"customer_id": 99, "dias": 7})
        query.assert_not_called()
        tool.invoke({"customer_id": 11, "dias": 7})
    assert query.call_args.args[1]["customer_id"] == 11


def test_customer_lookup_returns_own_identifier_without_directory_query(monkeypatch):
    query = Mock()
    monkeypatch.setattr(clientes, "executar_consulta", query)
    with identity_context(CUSTOMER):
        assert "11" in clientes.localizar_cliente.invoke({"email": "outra@pessoa.com"})
    query.assert_not_called()


def test_memory_scope_changes_with_role_or_sector_and_excludes_legacy(monkeypatch):
    persistence = Mock()
    persistence.list.return_value = iter([{"memories": [
        {"content": "gerencial", "access_scope": MANAGER.scope},
        {"content": "legado"}, {"content": "próprio", "access_scope": CUSTOMER.scope},
    ]}])
    monkeypatch.setattr(api_observability, "get_ai_api", lambda: persistence)
    with identity_context(CUSTOMER):
        assert [m["content"] for m in api_observability.carregar_memorias(7)] == ["próprio"]
        api_observability.registrar_memorias(7, [{"tipo": "fato", "conteudo": "próprio", "importancia": 1}])
    assert persistence.append_memories.call_args.args[1][0]["access_scope"] == CUSTOMER.scope
    assert STOCKER.scope != Principal(7, "STOCKER", sector_ids=(4,)).scope


@pytest.mark.parametrize("position,customer,role", [("MANAGER", None, "MANAGER"), ("STOCKER", None, "STOCKER"), (None, 11, "CUSTOMER")])
def test_profiles_resolve_from_real_database_links(monkeypatch, position, customer, role):
    query = Mock(side_effect=[[(7, customer, 8, position)], [(2,), (3,)]])
    monkeypatch.setattr(auth, "executar_consulta", query)
    result = auth.resolve_identity("pessoa@kairos.com")
    assert result.role == role
    assert query.call_args_list[0].args[1] == {"email": "pessoa@kairos.com"}
    if position == "STOCKER":
        assert result.sector_ids == (2, 3)


def test_cashier_is_not_implicitly_a_stocker(monkeypatch):
    monkeypatch.setattr(auth, "executar_consulta", lambda *_: [(7, None, 8, "CASHIER")])
    with pytest.raises(HTTPException) as error:
        auth.resolve_identity("caixa@kairos.com")
    assert error.value.status_code == 403


def test_spring_session_and_csrf_are_validated_by_backend(monkeypatch):
    monkeypatch.setattr(settings, "spring_auth_url", "http://localhost:8080")
    monkeypatch.setattr(settings, "chat_allowed_origins", ["http://localhost:5173"])
    calls = []

    def spring(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/v1/auth/chat-context"
        assert request.headers["Cookie"] == "JSESSIONID=existing"
        assert request.headers["X-CSRF-TOKEN"] == "csrf"
        return httpx.Response(200, json={"email": "gerente@kairos.com", "roles": ["MANAGER"]})

    client_type = httpx.Client
    monkeypatch.setattr(auth.httpx, "Client", lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(spring)))
    request = Request({"type": "http", "headers": [(b"cookie", b"JSESSIONID=existing; unrelated=secret"),
                       (b"x-csrf-token", b"csrf"), (b"origin", b"http://localhost:5173")]})
    assert auth.spring_identity(request) == "gerente@kairos.com"
    for headers, status in [([], 401), ([(b"cookie", b"JSESSIONID=existing")], 403),
                            ([(b"cookie", b"JSESSIONID=existing"), (b"x-csrf-token", b"csrf"), (b"origin", b"https://evil.example")], 403)]:
        with pytest.raises(HTTPException) as error:
            auth.spring_identity(Request({"type": "http", "headers": headers}))
        assert error.value.status_code == status
    assert len(calls) == 1


def test_mobile_checks_revocation_and_verified_email(monkeypatch):
    from firebase_admin import auth as firebase_auth
    monkeypatch.setattr(settings, "firebase_project_id", "kairos-test")
    app = object()
    monkeypatch.setattr(auth, "firebase_app", lambda: app)
    verify = Mock(return_value={"uid": "uid-string", "email": "cliente@kairos.com", "email_verified": True, "roles": ["MANAGER"]})
    monkeypatch.setattr(firebase_auth, "verify_id_token", verify)
    assert auth.firebase_identity("id-token") == "cliente@kairos.com"
    verify.assert_called_once_with("id-token", app=app, check_revoked=True)
    verify.return_value["email_verified"] = False
    with pytest.raises(HTTPException) as error:
        auth.firebase_identity("id-token")
    assert error.value.status_code == 403
    verify.side_effect = firebase_auth.RevokedIdTokenError("revoked")
    with pytest.raises(HTTPException) as error:
        auth.firebase_identity("id-token")
    assert error.value.status_code == 401


def test_specialist_does_not_invoke_forbidden_agent(monkeypatch):
    import graph_nodes
    finish = Mock()
    agent = Mock()
    monkeypatch.setattr(graph_nodes, "finalizar_execucao", finish)
    with identity_context(CUSTOMER), pytest.raises(PermissionError):
        graph_nodes._executar_especialista("vendas", agent, {"execution_id": ObjectId(ID)})
    agent.invoke.assert_not_called()
    assert finish.call_args.args[1] == "REJECTED"


def test_permission_context_propagates_to_langchain_workers():
    from langchain_core.runnables import RunnableLambda
    with identity_context(CUSTOMER):
        results = RunnableLambda(lambda _: require_domain("vendas")).batch([1, 2], return_exceptions=True)
    assert all(isinstance(result, PermissionError) for result in results)


def test_reading_memory_rechecks_scope_before_summarization(monkeypatch):
    persistence = Mock()
    persistence.conversation.return_value = {"user_id": 7, "access_scope": MANAGER.scope, "summary": "gerencial"}
    monkeypatch.setattr(settings, "persistence_backend", "api")
    monkeypatch.setattr(memory_store, "get_ai_api", lambda: persistence)
    with identity_context(CUSTOMER), pytest.raises(PermissionError):
        memory_store.obter_conversa_por_id(ObjectId(ID))


def test_public_auth_does_not_accept_internal_token_as_user_identity(client, monkeypatch):
    browser, persistence = client
    api.app.dependency_overrides.clear()
    verifier = Mock(side_effect=HTTPException(401, "inválido"))
    resolver = Mock()
    monkeypatch.setattr(auth, "firebase_identity", verifier)
    monkeypatch.setattr(auth, "resolve_identity", resolver)
    result = browser.get("/api/v1/me/chat", headers={"Authorization": "Bearer service-secret"})
    assert result.status_code == 401
    resolver.assert_not_called()
    persistence.request.assert_not_called()
