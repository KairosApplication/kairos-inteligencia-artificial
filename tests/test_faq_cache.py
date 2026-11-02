from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pytest

from app.tools.faq import CachedEmbeddings


def test_cache_reutiliza_vetores_entre_instancias(tmp_path):
    delegate = Mock()
    delegate.embed_documents.return_value = [[1.0, 0.0], [0.0, 1.0]]
    path = tmp_path / "cache.sqlite3"
    first = CachedEmbeddings(delegate, path, "modelo-a")
    assert first.embed_documents(["a", "b", "a"]) == [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]
    second = CachedEmbeddings(delegate, path, "modelo-a")
    assert second.embed_documents(["a"]) == [[1.0, 0.0]]
    delegate.embed_documents.assert_called_once_with(["a", "b"])


def test_cache_envia_somente_documentos_novos(tmp_path):
    delegate = Mock()
    delegate.embed_documents.side_effect = [[[1.0], [2.0]], [[3.0]]]
    cache = CachedEmbeddings(delegate, tmp_path / "cache.sqlite3", "modelo-a")
    assert cache.embed_documents(["a", "b"]) == [[1.0], [2.0]]
    assert cache.embed_documents(["b", "c", "a"]) == [[2.0], [3.0], [1.0]]
    assert [call.args[0] for call in delegate.embed_documents.call_args_list] == [["a", "b"], ["c"]]


def test_consultas_simultaneas_reutilizam_um_unico_embedding(tmp_path):
    delegate = Mock()
    delegate.embed_query.return_value = [1.0, 0.0]
    path = tmp_path / "cache.sqlite3"
    caches = [CachedEmbeddings(delegate, path, "modelo-a") for _ in range(4)]
    start = Barrier(len(caches))

    def query(cache):
        start.wait(timeout=5)
        return cache.embed_query("mesma pergunta")

    with ThreadPoolExecutor(max_workers=len(caches)) as pool:
        assert list(pool.map(query, caches)) == [[1.0, 0.0]] * len(caches)
    delegate.embed_query.assert_called_once_with("mesma pergunta")


def test_falha_nao_e_repetida_nem_armazenada_no_cache(tmp_path):
    delegate = Mock()
    delegate.embed_query.side_effect = [TimeoutError(), [1.0]]
    cache = CachedEmbeddings(delegate, tmp_path / "cache.sqlite3", "modelo-a")
    with pytest.raises(TimeoutError):
        cache.embed_query("pergunta")
    assert delegate.embed_query.call_count == 1
    assert cache.embed_query("pergunta") == [1.0]
    assert cache.embed_query("pergunta") == [1.0]
    assert delegate.embed_query.call_count == 2


def test_modelo_novo_nao_reutiliza_vetor_antigo(tmp_path):
    delegate = Mock()
    delegate.embed_query.side_effect = [[1.0], [2.0]]
    path = tmp_path / "cache.sqlite3"
    assert CachedEmbeddings(delegate, path, "a").embed_query("texto") == [1.0]
    assert CachedEmbeddings(delegate, path, "b").embed_query("texto") == [2.0]
    assert delegate.embed_query.call_count == 2


def test_cache_separa_consulta_de_documento_e_reutiliza_consulta(tmp_path):
    delegate = Mock()
    delegate.embed_documents.return_value = [[1.0, 0.0]]
    delegate.embed_query.return_value = [0.0, 1.0]
    path = tmp_path / "cache.sqlite3"
    first = CachedEmbeddings(delegate, path, "local")
    assert first.embed_documents(["texto"]) == [[1.0, 0.0]]
    assert first.embed_query("texto") == [0.0, 1.0]
    second = CachedEmbeddings(delegate, path, "local")
    assert second.embed_query("texto") == [0.0, 1.0]
    assert delegate.embed_query.call_count == 1


def test_busca_faq_informa_fonte_e_pagina(monkeypatch):
    from langchain_core.documents import Document
    from app.tools import faq
    db = Mock()
    db.max_marginal_relevance_search.return_value = [
        Document(page_content="O repositor confirma a reposição por escaneamento.", metadata={"page": 1})]
    monkeypatch.setattr(faq, "create_faq_database", lambda: db)
    result = faq.faq_retriever.invoke({"question": "Como confirmar a reposição?"})
    assert "[Fonte: FAQ Kairos, página 2]" in result
    assert "escaneamento" in result
