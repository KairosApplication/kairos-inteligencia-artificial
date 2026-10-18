from unittest.mock import Mock
from datetime import date
from decimal import Decimal

import pytest

from config import Settings
from data import postgres
from tools import clientes, estoque, vendas


@pytest.mark.parametrize("filters", [{"nome": "   "}, {"cpf": "abc"}, {"email": " "}, {"cpf": "123"}])
def test_cliente_sem_filtros_validos_nao_consulta(monkeypatch, filters):
    query = Mock()
    monkeypatch.setattr(clientes, "_executar_consulta", query)
    clientes.localizar_cliente.invoke(filters)
    query.assert_not_called()


def test_filtro_literal_nao_vira_curinga():
    assert clientes._like("%_") == "%\\%\\_%"


def test_nivel_90_porcento_e_adequado(monkeypatch):
    monkeypatch.setattr(estoque, "_executar_consulta", lambda *a: [("Arroz", 1, "Alimentos", 90, 100, .90)])
    assert "adequado" in estoque.consultar_niveis_estoque.invoke({})


@pytest.mark.parametrize("q,p,expected", [(0, 0, "ruptura"), (10, .1, "crítico"), (30, .3, "baixo"), (90, .9, "adequado"), (1, None, "indefinido")])
def test_severidade(q, p, expected):
    assert estoque._classificar_severidade(q, p, .2, .5) == expected


def test_erro_de_banco_nao_expoe_detalhes(monkeypatch):
    monkeypatch.setattr(clientes, "_executar_consulta", Mock(side_effect=RuntimeError("password=SECRET")))
    result = clientes.localizar_cliente.invoke({"nome": "Maria"})
    assert "SECRET" not in result
    assert "Não foi possível" in result


def test_ausencia_de_historico_nao_e_previsao_de_demanda_zero(monkeypatch):
    monkeypatch.setattr(vendas, "_serie_semanal_vendas", lambda *args: [])
    result = vendas.prever_demanda_futura.invoke({"produto": "Inexistente"})
    assert "Não é possível projetar a demanda sem histórico" in result
    assert "0 unidades" not in result


def test_pool_libera_conexao_e_fecha_cursor_apos_erro(monkeypatch):
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    cursor_context = Mock()
    cursor_context.__enter__ = Mock(return_value=cursor_context)
    cursor_context.__exit__ = Mock(return_value=False)
    cursor_context.execute.side_effect = RuntimeError("consulta inválida")
    connection.cursor.return_value = cursor_context
    monkeypatch.setattr(postgres, "get_connection", lambda: connection)
    release = Mock()
    monkeypatch.setattr(postgres, "release_connection", release)
    with pytest.raises(RuntimeError):
        postgres.executar_consulta("SELECT", {})
    release.assert_called_once_with(connection)
    cursor_context.__exit__.assert_called_once()
    connection.__exit__.assert_called_once()


def test_configuracao_rejeita_faixas_invalidas():
    with pytest.raises(ValueError):
        Settings(estoque_limite_critico=.8, estoque_limite_baixo=.2)


def test_localizar_cliente_retorna_identificacao(monkeypatch):
    monkeypatch.setattr(clientes, "_executar_consulta", lambda *args: [
        (7, "Maria", "Teste", "maria@example.invalid")])
    result = clientes.localizar_cliente.invoke({"nome": "Maria"})
    assert "customer_id: 7" in result
    assert "Maria Teste" in result


def test_historico_cliente_preserva_quantidades_e_valores(monkeypatch):
    query = Mock(side_effect=[
        [(1,)],
        [("Arroz", "Alimentos", 3, Decimal("75.00"), 2, date(2026, 10, 1))],
    ])
    monkeypatch.setattr(clientes, "_executar_consulta", query)
    result = clientes.consultar_historico_compras_cliente.invoke({"customer_id": 7})
    assert "Quantidade: 3" in result
    assert "R$ 75.00" in result
    assert "2026-10-01" in result


def test_ofertas_cliente_indica_promocao_e_motivo(monkeypatch):
    monkeypatch.setattr(clientes, "_executar_consulta", Mock(side_effect=[
        [(1,)],
        [("Arroz", "Alimentos", Decimal("10"), "Oferta semanal",
          date(2026, 10, 30), Decimal("25.00"), True)],
    ]))
    result = clientes.sugerir_ofertas_personalizadas.invoke({"customer_id": 7})
    assert "Desconto: 10%" in result
    assert "produto já comprado" in result


def test_historico_vendas_preserva_totais(monkeypatch):
    monkeypatch.setattr(vendas, "_executar_consulta", lambda *args: [
        ("Arroz", "Alimentos", 10, Decimal("250.00"), 4)])
    result = vendas.consultar_historico_vendas.invoke({"produto": "Arroz"})
    assert "Quantidade vendida: 10" in result
    assert "R$ 250.00" in result
    assert "Pedidos: 4" in result


def test_previsao_de_serie_linear_conhecida(monkeypatch):
    # Oito semanas completas: 10, 20, ..., 80 unidades.
    monkeypatch.setattr(vendas, "_serie_semanal_vendas", lambda *args: [
        (date(2026, 8, 3), 10 * i, i) for i in range(1, 9)])
    result = vendas.prever_demanda_futura.invoke({"produto": "Arroz", "semanas_previsao": 2})
    assert "Semana +1: 90 unidades" in result
    assert "Semana +2: 100 unidades" in result


def test_alertas_identifica_pendente_e_resolvido(monkeypatch):
    monkeypatch.setattr(estoque, "_executar_consulta", lambda *args: [
        (1, "Reposição", "OPEN", date(2026, 10, 1), None, "Arroz", 2, "Alimentos"),
        (2, "Reposição", "CLOSED", date(2026, 10, 1), date(2026, 10, 2), "Feijão", 3, "Alimentos"),
    ])
    result = estoque.listar_alertas_reposicao.invoke({"apenas_pendentes": False})
    assert "Situação: pendente" in result
    assert "Situação: resolvido" in result


def test_recomendacoes_explicita_ruptura_e_alerta(monkeypatch):
    monkeypatch.setattr(estoque, "_executar_consulta", lambda *args: [
        ("Arroz", 1, "Alimentos", 0, 100, 0, True),
        ("Feijão", 2, "Alimentos", 10, 100, .1, False),
    ])
    result = estoque.recomendar_prioridades_reposicao.invoke({})
    assert "Severidade: ruptura | Alerta ativo" in result
    assert "Severidade: crítico" in result
