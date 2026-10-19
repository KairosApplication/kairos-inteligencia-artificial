import json
from unittest.mock import Mock

import pytest
from bson import ObjectId
from langchain_core.messages import AIMessage

from data import conversation_middleware as memory
from data import memory_store, mongodb


def conversation(n=20):
    return {"_id": ObjectId(), "summary": None, "summarized_count": 0,
            "messages": [{"sender": "USER" if i % 2 == 0 else "AI", "content": str(i)} for i in range(n)]}


def test_compactacao_cobre_prefixo_correto_e_nao_repete(monkeypatch):
    conv = conversation()
    model = Mock(invoke=Mock(return_value=AIMessage(content="Resumo 0 a 15")))

    def store(conversation_id, summary, old, new, **kwargs):
        assert (old, new) == (0, 16)
        conv.update(summary=summary, summarized_count=new)
        return True

    monkeypatch.setattr(memory, "registrar_resumo", store)
    monkeypatch.setattr(memory, "obter_conversa_por_id", lambda _: conv)
    result = memory.compactar_conversa(conv, model)
    payload = json.loads(model.invoke.call_args.args[0][-1].content)
    assert [m["content"] for m in payload["mensagens_aprovadas"]] == [str(i) for i in range(16)]
    assert [m.content for m in memory_store.carregar_mensagens_contexto(result)][1:] == ["16", "17", "18", "19"]
    memory.compactar_conversa(result, model)
    assert model.invoke.call_count == 1


def test_falha_no_resumo_preserva_historico_e_marcador(monkeypatch):
    conv = conversation()
    write = Mock()
    monkeypatch.setattr(memory, "registrar_resumo", write)
    assert memory.compactar_conversa(conv, Mock(invoke=Mock(side_effect=RuntimeError("limit")))) == conv
    assert conv["summarized_count"] == 0
    assert len(memory_store.carregar_mensagens_contexto(conv)) == 20
    write.assert_not_called()


def test_disputa_nao_sobrescreve_resumo_novo(monkeypatch):
    conv = conversation()
    newer = {**conv, "summary": "Outra execução", "summarized_count": 16}
    monkeypatch.setattr(memory, "registrar_resumo", Mock(return_value=False))
    monkeypatch.setattr(memory, "obter_conversa_por_id", lambda _: newer)
    result = memory.compactar_conversa(conv, Mock(invoke=Mock(return_value=AIMessage(content="Resumo obsoleto"))))
    assert result["summary"] == "Outra execução"
    assert memory.registrar_resumo.call_count == 1


def test_historico_longo_e_resumido_em_lotes_sem_perdas(monkeypatch):
    conv = conversation(64)
    model = Mock(invoke=Mock(return_value=AIMessage(content="Resumo")))
    covered = []

    def store(conversation_id, summary, old, new, **kwargs):
        covered.extend(range(old, new))
        conv.update(summary=summary, summarized_count=new)
        return True

    monkeypatch.setattr(memory, "registrar_resumo", store)
    monkeypatch.setattr(memory, "obter_conversa_por_id", lambda _: conv)
    result = memory.compactar_conversa(conv, model)
    assert covered == list(range(60))
    assert result["summarized_count"] == 60
    assert model.invoke.call_count == 2
    payload = json.loads(model.invoke.call_args.args[0][-1].content)
    assert payload["resumo_anterior"] == "Resumo"
    assert [m["content"] for m in payload["mensagens_aprovadas"]] == [str(i) for i in range(40, 60)]
    assert [m.content for m in memory_store.carregar_mensagens_contexto(result)][1:] == ["60", "61", "62", "63"]


def test_mongodb_sem_nome_nao_conecta_ao_banco_padrao(monkeypatch):
    monkeypatch.setattr(mongodb.settings, "mongodb_database", None)
    client = Mock()
    monkeypatch.setattr(mongodb, "get_client", client)
    with pytest.raises(RuntimeError, match="MONGODB_DATABASE"):
        mongodb.get_database()
    client.assert_not_called()
