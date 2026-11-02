import statistics
from typing import Optional

from config import settings

from langchain.tools import tool

from data.postgres import executar_consulta
from data.access import require_domain, company_scope
from tools.query_policy import validar_limite, validar_periodo


# Demanda considera somente compras concluídas.
STATUS_VENDA_CONCLUIDA = "COMPLETED"


def _executar_consulta(sql: str, params: dict) -> list:
    require_domain("vendas")
    return executar_consulta(sql, {**params, "company_id": company_scope()})


def _like(valor: Optional[str]) -> Optional[str]:
    """Prepara busca parcial com ILIKE; retorna None para texto vazio."""
    if not valor or not valor.strip():
        return None

    return f"%{valor.strip()}%"


def _serie_semanal_vendas(
    produto: Optional[str],
    categoria: Optional[str],
    semanas: int
) -> list:
    """Retorna (semana, quantidade, índice) em ordem cronológica para vendas concluídas.
    Preenche semanas sem vendas com zero; o índice alimenta a regressão."""
    sql = """
        WITH semanas_periodo AS (
            SELECT generate_series(
                DATE_TRUNC('week', now() - (%(semanas)s * INTERVAL '1 week')),
                DATE_TRUNC('week', now()) - INTERVAL '1 week',
                INTERVAL '1 week'
            )::date AS semana
        ),
        vendas AS (
            SELECT
                DATE_TRUNC('week', p.date)::date AS semana,
                SUM(pp.quantity) AS quantidade_vendida
            FROM purchases p
            JOIN purchase_products pp ON pp.purchase_id = p.id
            JOIN products pr ON pr.id = pp.product_id
            LEFT JOIN categories c ON c.id = pr.category_id
            WHERE p.status = %(status_concluido)s
              AND (%(company_id)s::bigint IS NULL OR pr.company_id = %(company_id)s)
              AND (%(company_id)s::bigint IS NULL OR EXISTS (SELECT 1 FROM customers scope_customer WHERE scope_customer.id = p.customer_id AND scope_customer.company_id = %(company_id)s))
              AND p.date >= DATE_TRUNC('week', now() - (%(semanas)s * INTERVAL '1 week'))
              AND p.date < DATE_TRUNC('week', now())
              AND (%(produto)s IS NULL OR pr.name ILIKE %(produto)s)
              AND (%(categoria)s IS NULL OR c.category ILIKE %(categoria)s)
            GROUP BY semana
        ),
        serie AS (
            SELECT
                sp.semana,
                COALESCE(v.quantidade_vendida, 0) AS quantidade_vendida
            FROM semanas_periodo sp
            LEFT JOIN vendas v ON v.semana = sp.semana
        )
        SELECT
            semana,
            quantidade_vendida,
            ROW_NUMBER() OVER (ORDER BY semana) AS indice_semana
        FROM serie
        WHERE EXISTS (SELECT 1 FROM vendas)
        ORDER BY semana ASC
    """

    return _executar_consulta(sql, {
        "status_concluido": STATUS_VENDA_CONCLUIDA,
        "semanas": semanas,
        "produto": _like(produto),
        "categoria": _like(categoria)
    })


def _regressao_linear(indices: list, valores: list):
    """Retorna (inclinação, intercepto), ou (None, None) sem pontos suficientes/variância em x."""
    try:
        resultado = statistics.linear_regression(indices, valores)

    except statistics.StatisticsError:
        return None, None

    return resultado.slope, resultado.intercept


def _erro_padrao_inclinacao(
    indices: list, valores: list, inclinacao: float, intercepto: float
) -> Optional[float]:
    """Estima o erro padrão da inclinação pelos resíduos da série.
    Retorna None com menos de três pontos ou sem variância em x."""
    n = len(indices)

    if n < 3:
        return None

    media_x = statistics.mean(indices)
    soma_quadrados_x = sum((x - media_x) ** 2 for x in indices)

    if soma_quadrados_x == 0:
        return None

    soma_quadrados_residuos = sum(
        (y - (intercepto + inclinacao * x)) ** 2 for x, y in zip(indices, valores)
    )

    # n-2 graus de liberdade: inclinação e intercepto foram estimados.
    variancia_residual = soma_quadrados_residuos / (n - 2)

    return (variancia_residual / soma_quadrados_x) ** 0.5


def _classificar_tendencia(
    inclinacao: Optional[float], erro_padrao: Optional[float]
) -> str:
    """Classifica a inclinação em relação ao seu erro padrão.
    Sem erro padrão calculável, usa o sinal da inclinação."""
    if inclinacao is None:
        return "indefinida"

    if erro_padrao is None:
        if inclinacao == 0:
            return "estável"
        return "crescimento" if inclinacao > 0 else "queda"

    if erro_padrao == 0:
        if inclinacao == 0:
            return "estável"
        return "crescimento" if inclinacao > 0 else "queda"

    # Distância da inclinação a zero, medida em erros padrão.
    estatistica_t = inclinacao / erro_padrao

    # Aproximação normal de 95%; a dispersão da série já está no erro padrão.
    if abs(estatistica_t) < 1.96:
        return "estável"

    return "crescimento" if inclinacao > 0 else "queda"


@tool
def consultar_historico_vendas(
    produto: Optional[str] = None,
    categoria: Optional[str] = None,
    dias: Optional[int] = None,
    limite: Optional[int] = None
) -> str:
    """Consulta quantidade, receita e pedidos por produto em vendas concluídas nos últimos dias.
    produto e categoria aceitam nomes parciais; limite define o máximo de produtos.
    dias deve vir do usuário; se ausente, pede esclarecimento sem consultar o banco."""
    dias, aviso = validar_periodo(dias, "dias", settings.historico_vendas_max_dias, "De quantos dias anteriores você quer consultar as vendas?")
    if aviso:
        return aviso
    limite, aviso = validar_limite(limite)
    if aviso:
        return aviso

    sql = """
        SELECT
            pr.name AS produto,
            c.category AS categoria,
            SUM(pp.quantity) AS quantidade_vendida,
            SUM(pp.amount::numeric) AS valor_total,
            COUNT(DISTINCT pp.purchase_id) AS numero_pedidos
        FROM purchase_products pp
        JOIN purchases p ON p.id = pp.purchase_id
        JOIN products pr ON pr.id = pp.product_id
        LEFT JOIN categories c ON c.id = pr.category_id
        WHERE p.status = %(status_concluido)s
              AND (%(company_id)s::bigint IS NULL OR pr.company_id = %(company_id)s)
              AND (%(company_id)s::bigint IS NULL OR EXISTS (SELECT 1 FROM customers scope_customer WHERE scope_customer.id = p.customer_id AND scope_customer.company_id = %(company_id)s))
          AND p.date >= now() - (%(dias)s * INTERVAL '1 day')
          AND (%(produto)s IS NULL OR pr.name ILIKE %(produto)s)
          AND (%(categoria)s IS NULL OR c.category ILIKE %(categoria)s)
        GROUP BY pr.id, pr.name, c.category
        ORDER BY quantidade_vendida DESC
        LIMIT %(limite)s
    """

    try:
        linhas = _executar_consulta(sql, {
            "status_concluido": STATUS_VENDA_CONCLUIDA,
            "dias": dias,
            "produto": _like(produto),
            "categoria": _like(categoria),
            "limite": limite
        })

    except Exception:
        return "Não foi possível consultar o histórico de vendas. Tente novamente mais tarde."

    if not linhas:
        return f"Nenhuma venda concluída encontrada nos últimos {dias} dias para os filtros informados."

    resultados = [f"Histórico de vendas concluídas nos últimos {dias} dias (até {limite} produtos):"]

    for nome, categoria_nome, quantidade, valor_total, numero_pedidos in linhas:
        resultados.append(
            f"- Produto: {nome} | Categoria: {categoria_nome or 'não especificada'} | "
            f"Quantidade vendida: {quantidade} | Valor total: R$ {valor_total:.2f} | "
            f"Pedidos: {numero_pedidos}"
        )

    return "\n".join(resultados)


@tool
def analisar_tendencia_vendas(
    produto: Optional[str] = None,
    categoria: Optional[str] = None,
    semanas: Optional[int] = None
) -> str:
    """Analisa vendas concluídas por regressão linear; retorna série, média semanal e tendência.
    produto e categoria aceitam nomes parciais; semanas são semanas completas anteriores.
    Confirme semanas com o usuário; se ausente, pede esclarecimento sem consultar o banco."""
    semanas, aviso = validar_periodo(
        semanas, "semanas", settings.tendencia_max_semanas,
        "Quantas semanas completas você quer analisar?", minimo=3,
    )
    if aviso:
        return aviso

    try:
        linhas = _serie_semanal_vendas(produto, categoria, semanas)

    except Exception:
        return "Não foi possível analisar a tendência de vendas. Tente novamente mais tarde."

    if not linhas:
        return "Nenhum dado de vendas encontrado para o período e filtros informados."

    quantidades = [float(quantidade) for _, quantidade, _ in linhas]
    indices = [float(indice) for _, _, indice in linhas]

    media_semanal = statistics.mean(quantidades)
    inclinacao, intercepto = _regressao_linear(indices, quantidades)

    erro_padrao = (
        _erro_padrao_inclinacao(indices, quantidades, inclinacao, intercepto)
        if inclinacao is not None
        else None
    )

    tendencia = _classificar_tendencia(inclinacao, erro_padrao)

    resultados = [
        f"Tendência calculada sobre as últimas {len(linhas)} semanas: {tendencia}",
        f"Média semanal de vendas: {media_semanal:.1f} unidades"
    ]

    if inclinacao is not None:
        resultados.append(f"Variação média estimada por semana: {inclinacao:+.2f} unidades")
    else:
        resultados.append("Não há variação suficiente na série para estimar uma inclinação.")

    resultados.append("")
    resultados.append("Série semanal (semana | quantidade vendida):")

    for semana, quantidade, _ in linhas:
        resultados.append(f"- {semana}: {int(quantidade)}")

    return "\n".join(resultados)


@tool
def prever_demanda_futura(
    produto: Optional[str] = None,
    categoria: Optional[str] = None,
    semanas_historico: Optional[int] = None,
    semanas_previsao: Optional[int] = None
) -> str:
    """Estima vendas futuras por extrapolação linear do histórico de compras concluídas.
    É uma aproximação sem sazonalidade, promoções futuras ou eventos externos.
    produto e categoria aceitam nomes parciais. Confirme semanas_historico (completas)
    e semanas_previsao com o usuário; não invente períodos nem consulte sem ambos."""
    semanas_historico, aviso_historico = validar_periodo(
        semanas_historico, "semanas", settings.tendencia_max_semanas,
        "Quantas semanas completas de histórico devemos usar na previsão?", minimo=2,
    )
    semanas_previsao, aviso_previsao = validar_periodo(
        semanas_previsao, "semanas", settings.previsao_max_semanas,
        "Para quantas semanas futuras você quer a previsão?",
    )
    if aviso_historico or aviso_previsao:
        return " ".join(aviso for aviso in [aviso_historico, aviso_previsao] if aviso)

    try:
        linhas = _serie_semanal_vendas(produto, categoria, semanas_historico)

    except Exception:
        return "Não foi possível projetar a demanda. Tente novamente mais tarde."

    if not linhas:
        return (
            "Nenhum dado de vendas encontrado para o período e filtros "
            "informados. Não é possível projetar a demanda sem histórico."
        )

    quantidades = [float(quantidade) for _, quantidade, _ in linhas]
    indices = [float(indice) for _, _, indice in linhas]

    inclinacao, intercepto = _regressao_linear(indices, quantidades)

    if inclinacao is None:
        return (
            "Não há dados suficientes ou variação suficiente no histórico "
            "para projetar a demanda futura de forma confiável."
        )

    ultimo_indice = indices[-1]

    resultados = [
        f"Projeção de demanda por extrapolação linear sobre "
        f"{len(linhas)} semanas de histórico (estimativa aproximada, "
        f"não considera sazonalidade ou eventos externos):",
        ""
    ]

    for semana_futura in range(1, semanas_previsao + 1):
        indice_projetado = ultimo_indice + semana_futura
        quantidade_projetada = max(intercepto + inclinacao * indice_projetado, 0)

        resultados.append(
            f"- Semana +{semana_futura}: {quantidade_projetada:.0f} unidades (estimado)"
        )

    return "\n".join(resultados)


VENDAS_TOOLS = [
    consultar_historico_vendas,
    analisar_tendencia_vendas,
    prever_demanda_futura
]
