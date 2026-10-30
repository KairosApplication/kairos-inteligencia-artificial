"""Fontes reais, isolamento entre turnos e ausência de metadados privados na API."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, Mock

from bson import ObjectId
from langchain_core.messages import AIMessage, HumanMessage
import pytest

import api
import graph_nodes as nodes
from data import memory_store, postgres
from data.access import Principal
from data.answer_sources import capture_sources, consulted, public_sources
from data.conversation_middleware import MemoryContext
from schemas import AvaliacaoJuiz
from tools import faq

ID = ObjectId("000000000000000000000001")


def test_sources_do_not_accept_internal_names_or_metadata():
    assert public_sources(["database", "users.cpf", {"collection": "user_memory"}, "memory", "database"]) == ["database", "memory"]
    assert public_sources({"database": "sensitive"}) == []


def test_database_source_requires_successful_query(monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []
    monkeypatch.setattr(postgres, "get_connection", lambda: connection)
    monkeypatch.setattr(postgres, "release_connection", Mock())
    with capture_sources() as sources:
        postgres.executar_consulta("SELECT 1", {})
    assert sources == {"database"}
    cursor.execute.side_effect = RuntimeError("Unavailable")
    with capture_sources() as sources, pytest.raises(RuntimeError):
        postgres.executar_consulta("SELECT 1", {})
    assert sources == set()


def test_pdf_source_requires_retrieved_document(monkeypatch):
    database = Mock()
    monkeypatch.setattr(faq, "create_faq_database", lambda: database)
    database.max_marginal_relevance_search.return_value = []
    with capture_sources() as sources:
        faq.faq_retriever.invoke({"question": "Olá"})
    assert sources == set()
    database.max_marginal_relevance_search.return_value = [Mock(metadata={"page": 0}, page_content="Conteúdo público")]
    with capture_sources() as sources:
        faq.faq_retriever.invoke({"question": "Como funciona?"})
    assert sources == {"pdf"}


def test_consulted_sources_follow_langchain_workers_and_do_not_leak_between_requests():
    from langchain_core.runnables import RunnableLambda

    def request(source):
        with capture_sources() as sources:
            RunnableLambda(lambda _: consulted(source)).batch([1, 2])
        return public_sources(sources)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(request, ["pdf", "database"])) == [["pdf"], ["database"]]
    with capture_sources() as sources:
        pass
    assert sources == set()


@pytest.fixture
def state(monkeypatch):
    for name in ["registrar_trace_agente", "registrar_avaliacao_juiz", "finalizar_execucao", "_extrair_e_registrar_memorias"]:
        monkeypatch.setattr(nodes, name, Mock())
    return {"pergunta": "Qual o estoque?", "resposta": "Há produtos.", "execution_id": ID, "conversation_id": ID,
            "memory_context": MemoryContext(ID), "user_id": 1, "historico": [], "memorias_usuario": [], "tentativas": 0}


def test_model_cannot_invent_sources_and_retry_does_not_reuse_previous_sources(state):
    def answer(payload, **kwargs):
        return {"messages": [*payload["messages"], AIMessage(content="Consultei o banco.")]}
    agent = Mock(invoke=Mock(side_effect=answer))
    assert nodes._executar_especialista("estoque", agent, state)["sources"] == []
    state["sources"] = ["database"]
    assert nodes._executar_especialista("estoque", agent, state)["sources"] == []
    state["historico"] = [HumanMessage(content="Pergunta anterior")]
    assert nodes._executar_especialista("estoque", agent, state)["sources"] == ["memory"]


def test_approved_sources_are_persisted_and_rejected_sources_are_cleared(monkeypatch, state):
    save = Mock()
    monkeypatch.setattr(nodes, "registrar_turno", save)
    state["sources"] = ["database", "memory", "private.table"]
    monkeypatch.setattr(nodes, "juiz", Mock(invoke=Mock(return_value={"parsed": AvaliacaoJuiz(aprovado=True, motivo="Conferido")})))
    assert nodes.juiz_node(state)["sources"] == ["database", "memory"]
    save.assert_called_once_with(ID, state["pergunta"], state["resposta"], sources=["database", "memory"])
    nodes.juiz.invoke.return_value = {"parsed": AvaliacaoJuiz(aprovado=False, motivo="Não confirmado")}
    assert nodes.juiz_node(state)["sources"] == []


def test_source_metadata_is_saved_on_assistant_message_only(monkeypatch):
    database = MagicMock()
    monkeypatch.setattr(memory_store, "via_api", lambda: False)
    monkeypatch.setattr(memory_store, "get_database", lambda: database)
    memory_store.registrar_turno(ID, "Pergunta", "Resposta", sources=["pdf", "collection_name"])
    messages = database["conversations"].update_one.call_args.args[1]["$push"]["messages"]["$each"]
    assert "sources" not in messages[0]
    assert messages[1]["sources"] == ["pdf"]


def test_history_exposes_only_safe_source_categories(monkeypatch):
    identity = Principal(1, "MANAGER", company_id=1)
    persistence = Mock()
    persistence.conversation.return_value = {"_id": ID, "user_id": 1, "access_scope": identity.scope, "status": "ACTIVE",
        "messages": [{"sender": "AI", "content": "Resposta", "sources": ["pdf", "private_table"], "collection": "private"},
                     {"sender": "USER", "content": "Pergunta", "sources": ["database"]}]}
    monkeypatch.setattr(api, "get_ai_api", lambda: persistence)
    messages = api.my_conversation(str(ID), identity)["messages"]
    assert messages[0] == {"sender": "AI", "content": "Resposta", "timestamp": None, "sources": ["pdf"]}
    assert messages[1]["sources"] == []


@pytest.mark.parametrize("approved,blocked,expected", [(True, False, ["database"]), (False, False, []), (True, True, [])])
def test_message_response_never_exposes_unverified_or_raw_sources(monkeypatch, approved, blocked, expected):
    persistence = Mock()
    persistence.active_conversation.return_value = {"_id": ID}
    monkeypatch.setattr(api, "get_ai_api", lambda: persistence)
    monkeypatch.setattr(api, "execute_graph", lambda _: {"resposta": "Resposta", "execution_id": ID,
        "aprovado": approved, "bloqueado": blocked, "sources": ["database", "postgresql.users"]})
    result = api.process_message(api.ChatRequest(userId=1, question="Pergunta"))
    assert result.sources == expected
