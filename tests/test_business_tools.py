from unittest.mock import Mock
from datetime import date
from decimal import Decimal

import pytest

from config import Settings, settings
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
    result = vendas.prever_demanda_futura.invoke({"produto": "Inexistente", "semanas_historico": 8, "semanas_previsao": 2})
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


def test_postgresql_sem_nome_nao_usa_banco_implicito(monkeypatch):
    monkeypatch.delenv("POSTGRESQL_DB", raising=False)
    monkeypatch.setattr(postgres, "connection_pool", None)
    create_pool = Mock()
    monkeypatch.setattr(postgres.pool, "ThreadedConnectionPool", create_pool)
    with pytest.raises(RuntimeError, match="POSTGRESQL_DB"):
        postgres.get_connection()
    create_pool.assert_not_called()


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
    result = clientes.consultar_historico_compras_cliente.invoke({"customer_id": 7, "dias": 30})
    assert "Quantidade: 3" in result
    assert "R$ 75.00" in result
    assert "2026-10-01" in result


def test_ofertas_cliente_indica_promocao_e_motivo(monkeypatch):
    monkeypatch.setattr(clientes, "_executar_consulta", Mock(side_effect=[
        [(1,)],
        [("Arroz", "Alimentos", Decimal("10"), "Oferta semanal",
          date(2026, 10, 30), Decimal("25.00"), True)],
    ]))
    result = clientes.sugerir_ofertas_personalizadas.invoke({"customer_id": 7, "dias": 180})
    assert "Desconto: 10%" in result
    assert "produto já comprado" in result


def test_historico_vendas_preserva_totais(monkeypatch):
    monkeypatch.setattr(vendas, "_executar_consulta", lambda *args: [
        ("Arroz", "Alimentos", 10, Decimal("250.00"), 4)])
    result = vendas.consultar_historico_vendas.invoke({"produto": "Arroz", "dias": 30})
    assert "Quantidade vendida: 10" in result
    assert "R$ 250.00" in result
    assert "Pedidos: 4" in result


def test_previsao_de_serie_linear_conhecida(monkeypatch):
    # Oito semanas completas: 10, 20, ..., 80 unidades.
    monkeypatch.setattr(vendas, "_serie_semanal_vendas", lambda *args: [
        (date(2026, 8, 3), 10 * i, i) for i in range(1, 9)])
    result = vendas.prever_demanda_futura.invoke({"produto": "Arroz", "semanas_historico": 8, "semanas_previsao": 2})
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


@pytest.mark.parametrize("tool,params", [
    (vendas.consultar_historico_vendas, {}),
    (vendas.analisar_tendencia_vendas, {}),
    (vendas.prever_demanda_futura, {"semanas_previsao": 2}),
    (vendas.prever_demanda_futura, {"semanas_historico": 8}),
    (clientes.consultar_historico_compras_cliente, {"customer_id": 7}),
    (clientes.sugerir_ofertas_personalizadas, {"customer_id": 7}),
])
def test_periodo_ausente_pede_esclarecimento_sem_banco(monkeypatch, tool, params):
    queries = [Mock(), Mock()]
    monkeypatch.setattr(vendas, "_executar_consulta", queries[0])
    monkeypatch.setattr(clientes, "_executar_consulta", queries[1])
    result = tool.invoke(params)
    assert "Nenhuma consulta foi executada" in result
    assert "?" in result
    for query in queries:
        query.assert_not_called()


@pytest.mark.parametrize("tool,params", [
    (vendas.consultar_historico_vendas, {"dias": settings.historico_vendas_max_dias + 1}),
    (vendas.analisar_tendencia_vendas, {"semanas": settings.tendencia_max_semanas + 1}),
    (vendas.prever_demanda_futura, {"semanas_historico": 8, "semanas_previsao": settings.previsao_max_semanas + 1}),
    (clientes.consultar_historico_compras_cliente, {"customer_id": 7, "dias": settings.historico_clientes_max_dias + 1}),
])
def test_periodo_excessivo_nao_e_reduzido_silenciosamente(monkeypatch, tool, params):
    queries = [Mock(), Mock()]
    monkeypatch.setattr(vendas, "_executar_consulta", queries[0])
    monkeypatch.setattr(clientes, "_executar_consulta", queries[1])
    result = tool.invoke(params)
    assert "acima do limite" in result
    assert "Nenhuma consulta foi executada" in result
    for query in queries:
        query.assert_not_called()


@pytest.mark.parametrize("module,tool,params", [
    (estoque, estoque.consultar_niveis_estoque, {}),
    (estoque, estoque.listar_alertas_reposicao, {}),
    (estoque, estoque.recomendar_prioridades_reposicao, {}),
    (vendas, vendas.consultar_historico_vendas, {"dias": 30}),
    (clientes, clientes.localizar_cliente, {"nome": "Maria"}),
    (clientes, clientes.consultar_historico_compras_cliente, {"customer_id": 7, "dias": 30}),
    (clientes, clientes.sugerir_ofertas_personalizadas, {"customer_id": 7, "dias": 30}),
])
def test_limite_excessivo_bloqueia_todas_as_listagens(monkeypatch, module, tool, params):
    query = Mock()
    monkeypatch.setattr(module, "_executar_consulta", query)
    result = tool.invoke({**params, "limite": settings.limite_maximo_resultados + 1})
    assert "não foi reduzido automaticamente" in result
    query.assert_not_called()


@pytest.mark.parametrize("dias", [0, -1])
def test_periodo_invalido_nao_vira_padrao(monkeypatch, dias):
    query = Mock()
    monkeypatch.setattr(vendas, "_executar_consulta", query)
    result = vendas.consultar_historico_vendas.invoke({"dias": dias})
    assert "pelo menos 1 dia" in result
    query.assert_not_called()


def test_preferencia_usuario_chega_ao_banco(monkeypatch):
    query = Mock(return_value=[])
    monkeypatch.setattr(vendas, "_executar_consulta", query)
    vendas.consultar_historico_vendas.invoke({"dias": 45, "limite": 7})
    assert query.call_args.args[1]["dias"] == 45
    assert query.call_args.args[1]["limite"] == 7
