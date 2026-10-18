from unittest.mock import Mock

from tools.faq import CachedEmbeddings


def test_cache_reutiliza_vetores_entre_instancias(tmp_path):
    delegate = Mock()
    delegate.embed_documents.return_value = [[1.0, 0.0], [0.0, 1.0]]
    path = tmp_path / "cache.sqlite3"
    first = CachedEmbeddings(delegate, path, "modelo-a")
    assert first.embed_documents(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    second = CachedEmbeddings(delegate, path, "modelo-a")
    assert second.embed_query("a") == [1.0, 0.0]
    assert delegate.embed_documents.call_count == 1


def test_modelo_novo_nao_reutiliza_vetor_antigo(tmp_path):
    delegate = Mock()
    delegate.embed_documents.side_effect = [[[1.0]], [[2.0]]]
    path = tmp_path / "cache.sqlite3"
    assert CachedEmbeddings(delegate, path, "a").embed_query("texto") == [1.0]
    assert CachedEmbeddings(delegate, path, "b").embed_query("texto") == [2.0]
    assert delegate.embed_documents.call_count == 2


def test_busca_faq_informa_fonte_e_pagina(monkeypatch):
    from langchain_core.documents import Document
    from tools import faq
    db = Mock()
    db.max_marginal_relevance_search.return_value = [
        Document(page_content="O repositor confirma a reposição por escaneamento.", metadata={"page": 1})]
    monkeypatch.setattr(faq, "create_faq_database", lambda: db)
    result = faq.faq_retriever.invoke({"question": "Como confirmar a reposição?"})
    assert "[Fonte: FAQ Kairos, página 2]" in result
    assert "escaneamento" in result
