import statistics
from typing import Optional

from config import settings

from langchain.tools import tool

from data.postgres import executar_consulta
from tools.query_policy import validar_limite, validar_periodo


# Status que representa uma compra efetivamente concluída.
# IN_PROGRESS e CANCELLED não são consideradas venda para fins de
# análise de demanda.
STATUS_VENDA_CONCLUIDA = "COMPLETED"


def _executar_consulta(sql: str, params: dict) -> list:
    return executar_consulta(sql, params)


def _like(valor: Optional[str]) -> Optional[str]:
    """
    Prepara um valor de texto para busca parcial com ILIKE.
    Retorna None quando o valor não foi informado.
    """
    if not valor or not valor.strip():
        return None

    return f"%{valor.strip()}%"


def _serie_semanal_vendas(
    produto: Optional[str],
    categoria: Optional[str],
    semanas: int
) -> list:
    """
    Retorna a série semanal contínua de quantidade vendida (compras
    concluídas) para o período informado, preenchendo com zero as
    semanas sem venda registrada.

    Cada item da lista retornada é uma tupla
    (semana, quantidade_vendida, indice_semana), ordenada da semana
    mais antiga para a mais recente. O índice da semana é utilizado
    como variável independente na regressão linear.
    """
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
    """
    Calcula a inclinação e o intercepto de uma regressão linear
    simples (mínimos quadrados) sobre os pontos informados, utilizando
    statistics.linear_regression da biblioteca padrão do Python.

    Retorna (None, None) quando não há pontos suficientes ou quando
    todos os índices são iguais (variância nula em x, o que tornaria
    o cálculo indefinido).
    """
    try:
        resultado = statistics.linear_regression(indices, valores)

    except statistics.StatisticsError:
        return None, None

    return resultado.slope, resultado.intercept


def _erro_padrao_inclinacao(
    indices: list, valores: list, inclinacao: float, intercepto: float
) -> Optional[float]:
    """
    Calcula o erro padrão da inclinação de uma regressão linear
    simples, a partir dos resíduos observados na própria série
    (dispersão real dos pontos em torno da reta ajustada).

    Esse valor mede o quanto a inclinação estimada pode variar
    por acaso, dado o ruído característico desta série específica;
    é a base para `_classificar_tendencia` decidir se a inclinação
    observada é grande o suficiente, em relação ao próprio ruído da
    série, para ser chamada de tendência (em vez de comparar contra
    um percentual fixo arbitrário).

    Retorna None quando não há graus de liberdade suficientes (menos
    de 3 pontos) ou quando os índices não variam (nesse caso
    `_regressao_linear` já teria retornado None).
    """
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

    # Variância residual (erro quadrático médio), com n-2 graus de
    # liberdade (dois parâmetros estimados: inclinação e intercepto).
    variancia_residual = soma_quadrados_residuos / (n - 2)

    return (variancia_residual / soma_quadrados_x) ** 0.5


def _classificar_tendencia(
    inclinacao: Optional[float], erro_padrao: Optional[float]
) -> str:
    """
    Classifica a tendência de vendas com base na inclinação da reta
    de regressão linear sobre a série semanal, julgada em relação ao
    seu próprio erro padrão (ver `_erro_padrao_inclinacao`) — ou
    seja, em relação ao ruído real observado nesta série específica,
    em vez de um percentual fixo arbitrário aplicado a todo produto
    ou categoria indistintamente.

    Quando o erro padrão não pôde ser calculado (série muito curta),
    cai no critério mais simples de sinal da inclinação.
    """
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

    # Estatística de teste: quantos erros padrão a inclinação
    # observada está distante de zero. Abaixo do limiar de
    # significância, a inclinação não é distinguível de ruído.
    estatistica_t = inclinacao / erro_padrao

    # 1.96 é o múltiplo convencional de erro padrão para um teste de
    # significância aproximado a ~95% de confiança (distribuição
    # normal), não um valor calibrado nos dados do Kairos: a
    # dispersão real da série (quem de fato varia de produto para
    # produto) já foi incorporada no cálculo de `erro_padrao`.
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
    """
    Consulta o histórico de vendas concluídas do Kairos em um
    período recente.

    Retorna, por produto, a quantidade total vendida, o valor total
    em vendas e o número de pedidos distintos que incluíram o
    produto, considerando somente compras com status concluído.

    Parâmetros:
        produto: filtra pelo nome (ou parte do nome) do produto.
        categoria: filtra pela categoria (ou parte do nome) do produto.
        dias: últimos N dias, conforme pedido do usuário; se ausente,
            pede esclarecimento sem consultar o banco.
        limite: quantidade máxima de produtos retornados.
    """
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
    """
    Analisa a tendência de vendas de um produto ou categoria ao
    longo das últimas semanas, com base em regressão linear simples
    sobre a série semanal de quantidade vendida (compras concluídas).

    Retorna a série semanal encontrada, a média semanal de vendas e
    a classificação da tendência (crescimento, queda ou estável).

    Parâmetros:
        produto: filtra pelo nome (ou parte do nome) do produto.
        categoria: filtra pela categoria (ou parte do nome) do produto.
        semanas: semanas completas anteriores, conforme pedido do usuário;
            se ausente, pede esclarecimento sem consultar o banco.
    """
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
    """
    Projeta a quantidade de vendas esperada para as próximas semanas,
    extrapolando a reta de regressão linear calculada sobre o
    histórico semanal de vendas concluídas.

    Esta é uma projeção estatística simples (extrapolação linear),
    e não um modelo de previsão de demanda avançado. Deve ser
    apresentada como uma estimativa aproximada baseada apenas na
    tendência recente, sem considerar sazonalidade, promoções
    futuras ou eventos externos.

    Parâmetros:
        produto: filtra pelo nome (ou parte do nome) do produto.
        categoria: filtra pela categoria (ou parte do nome) do produto.
        semanas_historico: semanas completas de histórico confirmadas
            pelo usuário; não invente um valor quando ausente.
        semanas_previsao: semanas futuras confirmadas pelo usuário;
            ambos os períodos são necessários antes de consultar o banco.
    """
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
