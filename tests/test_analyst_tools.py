"""Contratos das tools e fronteiras de acesso, sem modelos ou serviços reais."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from pydantic import SecretStr

import api
from config import settings
from data import analyst_a2a, analyst_mcp, analyst_runtime, auth
from data.access import Principal
from data.databricks import DatabricksReader, DatabricksSettings, DatabricksUnavailable
from data.request_context import ChatContext, chat_context


def config(**overrides):
    return DatabricksSettings(databricks_host="https://example.cloud.databricks.com/path?o=123",
                              databricks_client_id="test-client", databricks_client_secret="test-secret",
                              sql_warehouse_id="test-warehouse", daily_sales="test.gold.sales", **overrides)


def reader(columns=("company_id", "sale_date", "total"), metadata_status=200, **overrides):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/oidc/v1/token":
            assert b"all-apis" not in request.content
            return httpx.Response(200, json={"access_token": "test-access", "expires_in": 3600})
        if request.method == "GET":
            return httpx.Response(metadata_status, json={"columns": [{"name": name} for name in columns],
                                                        "message": "SECRET_PROVIDER_ERROR"})
        return httpx.Response(200, json={"status": {"state": "SUCCEEDED"},
                                        "manifest": {"schema": {"columns": [{"name": "total"}]}},
                                        "result": {"data_array": [["25.00"]]}})

    return DatabricksReader(config(**overrides), httpx.Client(transport=httpx.MockTransport(handle))), calls


def test_gold_uses_fixed_select_parameters_scope_and_result_limits():
    source, calls = reader()
    result = source.indicator("vendas_diarias", 7, "2026-10-01", "2026-10-07", 3)
    request = json.loads(calls[-1].content)
    assert request["statement"] == "SELECT * FROM `test`.`gold`.`sales` WHERE `company_id` = :company AND CAST(`sale_date` AS DATE) BETWEEN :start AND :end ORDER BY `sale_date` DESC LIMIT 3"
    assert request["parameters"][0] == {"name": "company", "type": "BIGINT", "value": "7"}
    assert request["on_wait_timeout"] == "CANCEL"
    assert request["row_limit"] == 3
    assert result["registros"] == [{"total": "25.00"}]
    assert "test.gold.sales" not in json.dumps(result)
    assert source.config.databricks_host == "https://example.cloud.databricks.com"


@pytest.mark.parametrize("indicator,start,end,limit", [
    ("vendas_diarias; DROP TABLE sales", None, None, 3),
    ("vendas_diarias", None, None, 3),
    ("vendas_diarias", "2026-10-07", "2026-10-01", 3),
    ("vendas_diarias", "2024-01-01", "2026-10-01", 3),
    ("vendas_diarias", "2026-10-01", "2026-10-07", 500),
])
def test_invalid_queries_do_not_reach_provider(indicator, start, end, limit):
    source, calls = reader()
    with pytest.raises(DatabricksUnavailable):
        source.indicator(indicator, 7, start, end, limit)
    assert not calls


def test_missing_company_column_is_denied_before_any_sql():
    source, calls = reader(columns=("sale_date", "total"), databricks_company_id=None)
    with pytest.raises(DatabricksUnavailable, match="escopo de empresa"):
        source.indicator("vendas_diarias", 7, "2026-10-01", "2026-10-07")
    assert not any(request.url.path == "/api/2.0/sql/statements" for request in calls)


def test_exclusive_dataset_denies_other_company_before_authentication():
    source, calls = reader(databricks_company_id=7)
    with pytest.raises(DatabricksUnavailable, match="sua empresa"):
        source.indicator("vendas_diarias", 8, "2026-10-01", "2026-10-07")
    assert not calls


def test_provider_403_has_no_raw_error_and_does_not_start_warehouse():
    source, calls = reader(metadata_status=403)
    with pytest.raises(DatabricksUnavailable, match="autorização") as error:
        source.indicator("vendas_diarias", 7, "2026-10-01", "2026-10-07")
    assert "SECRET_PROVIDER_ERROR" not in str(error.value)
    assert not any(request.url.path == "/api/2.0/sql/statements" for request in calls)


@pytest.fixture
def analyst_key(monkeypatch):
    monkeypatch.setattr(settings, "analyst_a2a_token", SecretStr("test-analyst-key-with-at-least-32-characters"))


def test_mcp_protocol_tools_use_server_company_not_model_identity(monkeypatch, analyst_key):
    source = Mock()
    source.indicator.return_value = {"ok": True, "fonte": "database", "registros": []}
    monkeypatch.setattr(analyst_mcp, "get_databricks", lambda: source)

    async def run():
        async with Client(analyst_mcp.server) as client:
            tools = (await client.list_tools()).tools
            assert {tool.name for tool in tools} == {"consultar_indicador", "consultar_dashboard"}
            for tool in tools:
                assert tool.annotations.read_only_hint
                assert "company_id" not in tool.input_schema.get("properties", {})
                assert "sql" not in tool.input_schema.get("properties", {})
            result = await client.call_tool("consultar_indicador", {"indicador": "rupturas", "limite": 3},
                                           meta={"kairos_identity": auth.create_analyst_token(Principal(42, "MANAGER", company_id=7))})
            assert result.structured_content["ok"] is True

    asyncio.run(run())
    source.indicator.assert_called_once_with("rupturas", 7, None, None, 3)


@pytest.fixture
def a2a(monkeypatch, analyst_key):
    monkeypatch.setattr(settings, "analyst_a2a_url", "http://127.0.0.1/a2a/analyst")
    runtime = AsyncMock(return_value={"answer": "Teste de contrato.", "evidence": [], "sources": []})
    monkeypatch.setattr(analyst_runtime, "run_analyst", runtime)
    return runtime


def test_real_a2a_sdk_round_trip_preserves_verified_identity(a2a):
    async def run():
        async with api.app.router.lifespan_context(api.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://127.0.0.1") as http:
                return await analyst_a2a.request_analysis("Compare o histórico com o estoque.", Principal(42, "MANAGER", company_id=7), http)

    assert asyncio.run(run())["answer"] == "Teste de contrato."
    identity = a2a.call_args.args[1]
    assert identity == Principal(42, "MANAGER", company_id=7)


def test_application_starts_and_closes_mcp_without_subprocess(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=AssertionError("Subprocesso inesperado")))
    with TestClient(api.app) as client:
        assert client.get("/health").status_code == 200
        assert isinstance(api.app.state.analyst_mcp, Client)
        assert api.app.state.analyst_loop_token is not None
    assert api.app.state.analyst_mcp is None
    assert api.app.state.analyst_loop_token is None


def test_integrated_a2a_starts_lifecycle_automatically_for_console(a2a, monkeypatch):
    monkeypatch.setattr(settings, "analyst_a2a_url", None)
    result = asyncio.run(analyst_a2a.request_analysis("Uma análise.", Principal(42, "MANAGER", company_id=7)))
    assert result["answer"] == "Teste de contrato."
    assert isinstance(a2a.call_args.args[2], Client)
    assert api.app.state.analyst_mcp is None


@pytest.mark.parametrize("metadata", [{}, {"kairos_identity": "forged-token"}, {"company_id": 7}])
def test_mcp_rejects_missing_or_forged_identity_before_provider(monkeypatch, analyst_key, metadata):
    source = Mock()
    monkeypatch.setattr(analyst_mcp, "get_databricks", lambda: source)

    async def run():
        async with Client(analyst_mcp.server) as client:
            result = await client.call_tool("consultar_indicador", {"indicador": "rupturas"}, meta=metadata)
            assert result.structured_content["ok"] is False

    asyncio.run(run())
    source.indicator.assert_not_called()


def test_shared_mcp_connection_keeps_concurrent_companies_isolated(monkeypatch, analyst_key):
    source = Mock()
    source.indicator.side_effect = lambda metric, company, *args: {"ok": True, "empresa_validada": company}
    monkeypatch.setattr(analyst_mcp, "get_databricks", lambda: source)

    async def run():
        async with api.app.router.lifespan_context(api.app):
            async def query(company):
                identity = Principal(42, "MANAGER", company_id=company)
                result = await api.app.state.analyst_mcp.call_tool("consultar_indicador", {"indicador": "rupturas"},
                                                                 meta={"kairos_identity": auth.create_analyst_token(identity)})
                return result.structured_content["empresa_validada"]
            return await asyncio.gather(query(7), query(8), query(7), query(8))

    assert asyncio.run(run()) == [7, 8, 7, 8]


def test_sync_graph_reuses_application_loop_and_mcp(a2a, monkeypatch):
    from langchain_core.messages import HumanMessage
    monkeypatch.setattr(settings, "analyst_a2a_url", None)
    identity = Principal(42, "MANAGER", company_id=7)

    async def run():
        import anyio
        async with api.app.router.lifespan_context(api.app):
            client = api.app.state.analyst_mcp
            def invoke():
                token = chat_context.set(ChatContext(42, None, "test", identity))
                try:
                    return analyst_a2a.invoke_analyst({"messages": [HumanMessage(content="Uma análise.")]})
                finally:
                    chat_context.reset(token)
            result = await anyio.to_thread.run_sync(invoke)
            assert a2a.call_args.args[2] is client
            return result

    assert asyncio.run(run())["messages"][-1].text == "Teste de contrato."


@pytest.mark.parametrize("role,expired,expected", [("STOCKER", False, 403), ("CUSTOMER", False, 403), ("MANAGER", True, 401)])
def test_a2a_rejects_unprivileged_or_expired_credentials_before_agent(a2a, role, expired, expected):
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": "42", "role": role, "company_id": 7, "iat": now - timedelta(minutes=5),
                        "exp": now + timedelta(minutes=-1 if expired else 1), "iss": "kairos-chatbot", "aud": "kairos-analyst"},
                       settings.analyst_a2a_token.get_secret_value(), algorithm="HS256")
    with TestClient(api.app) as client:
        response = client.post("/a2a/analyst", headers={"Authorization": "Bearer " + token}, json={})
    assert response.status_code == expected
    a2a.assert_not_called()


def test_a2a_is_not_an_unauthenticated_model_endpoint(a2a):
    with TestClient(api.app) as client:
        assert client.post("/a2a/analyst", json={"company_id": 7, "role": "MANAGER"}).status_code == 401
        card = client.get("/a2a/analyst/.well-known/agent-card.json").json()
        assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
        assert settings.analyst_a2a_token.get_secret_value() not in json.dumps(card)
    a2a.assert_not_called()


@pytest.mark.parametrize("identity", [None, Principal(42, "CUSTOMER", company_id=7), Principal(42, "STOCKER", company_id=7)])
def test_analyst_denies_missing_or_non_manager_identity(identity):
    token = chat_context.set(ChatContext(42, None, "test", identity))
    try:
        with pytest.raises(PermissionError):
            analyst_a2a.invoke_analyst({"messages": []})
    finally:
        chat_context.reset(token)
