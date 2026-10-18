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
