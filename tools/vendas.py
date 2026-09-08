import statistics
from typing import Optional

from dotenv import load_dotenv
from langchain.tools import tool

from data.postgres import get_connection, release_connection

load_dotenv()

# Status que representa uma compra efetivamente concluída.
# IN_PROGRESS e CANCELLED não são consideradas venda para fins de
# análise de demanda.
STATUS_VENDA_CONCLUIDA = "COMPLETED"

LIMITE_PADRAO_RESULTADOS = 20
LIMITE_MAXIMO_RESULTADOS = 50

DIAS_PADRAO_HISTORICO = 30
DIAS_MAXIMO_HISTORICO = 365

SEMANAS_PADRAO_TENDENCIA = 8
SEMANAS_MAXIMA_TENDENCIA = 52

SEMANAS_PADRAO_PREVISAO = 4
SEMANAS_MAXIMA_PREVISAO = 12

# Limiar relativo à média semanal utilizado para classificar a
# tendência como "estável". Variações de inclinação menores que este
# percentual da média semanal são consideradas ruído, não tendência.
LIMIAR_TENDENCIA_ESTAVEL = 0.05


def _executar_consulta(sql: str, params: dict) -> list:
    """
    Executa uma consulta SQL de leitura utilizando uma conexão obtida
    do pool de conexões, garantindo que a conexão seja liberada de
    volta ao pool ao final, mesmo em caso de erro.
    """
    conn = get_connection()

    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        linhas = cur.fetchall()
        cur.close()
        return linhas

    finally:
        release_connection(conn)


def _like(valor: Optional[str]) -> Optional[str]:
    """
    Prepara um valor de texto para busca parcial com ILIKE.
    Retorna None quando o valor não foi informado.
    """
    if not valor or not valor.strip():
        return None

    return f"%{valor.strip()}%"


def _normalizar_intervalo(valor: int, padrao: int, maximo: int) -> int:
    """
    Garante que um intervalo numérico (dias, semanas, limite) esteja
    dentro de uma faixa segura, evitando consultas sem limite
    definido ou períodos exagerados.
    """
    if not valor or valor <= 0:
        return padrao

    return min(valor, maximo)


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
                DATE_TRUNC('week', now()),
                INTERVAL '1 week'
            )::date AS semana
        ),
        vendas AS (
            SELECT
                DATE_TRUNC('week', p.date)::date AS semana,
                SUM(pp.quantity) AS quantidade_vendida
            FROM purchase p
            JOIN purchase_product pp ON pp.purchase_id = p.id
            JOIN product pr ON pr.id = pp.product_id
            LEFT JOIN category c ON c.id = pr.category_id
            WHERE p.status = %(status_concluido)s
              AND p.date >= DATE_TRUNC('week', now() - (%(semanas)s * INTERVAL '1 week'))
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


def _classificar_tendencia(inclinacao: Optional[float], media_semanal: Optional[float]) -> str:
    """
    Classifica a tendência de vendas com base na inclinação da reta
    de regressão linear sobre a série semanal, relativizada pela
    média semanal de vendas.

    Isso evita classificar como "crescimento" ou "queda" variações
    pequenas em relação ao volume normalmente vendido.
    """
    if inclinacao is None or media_semanal is None:
        return "indefinida"

    if media_semanal == 0:
        if inclinacao == 0:
            return "estável"
        return "crescimento" if inclinacao > 0 else "queda"

    variacao_relativa = inclinacao / media_semanal

    if abs(variacao_relativa) < LIMIAR_TENDENCIA_ESTAVEL:
        return "estável"

    return "crescimento" if inclinacao > 0 else "queda"


@tool
def consultar_historico_vendas(
    produto: Optional[str] = None,
    categoria: Optional[str] = None,
    dias: int = DIAS_PADRAO_HISTORICO,
    limite: int = LIMITE_PADRAO_RESULTADOS
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
        dias: quantidade de dias anteriores à data atual considerados
            no histórico.
        limite: quantidade máxima de produtos retornados.
    """
    dias = _normalizar_intervalo(dias, DIAS_PADRAO_HISTORICO, DIAS_MAXIMO_HISTORICO)
    limite = _normalizar_intervalo(limite, LIMITE_PADRAO_RESULTADOS, LIMITE_MAXIMO_RESULTADOS)

    sql = """
        SELECT
            pr.name AS produto,
            c.category AS categoria,
            SUM(pp.quantity) AS quantidade_vendida,
            SUM(pp.amount::numeric) AS valor_total,
            COUNT(DISTINCT pp.purchase_id) AS numero_pedidos
        FROM purchase_product pp
        JOIN purchase p ON p.id = pp.purchase_id
        JOIN product pr ON pr.id = pp.product_id
        LEFT JOIN category c ON c.id = pr.category_id
        WHERE p.status = %(status_concluido)s
          AND p.date >= now() - (%(dias)s * INTERVAL '1 day')
          AND (%(produto)s IS NULL OR pr.name ILIKE %(produto)s)
          AND (%(categoria)s IS NULL OR c.category ILIKE %(categoria)s)
        GROUP BY pr.name, c.category
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

    except Exception as erro:
        return f"Não foi possível consultar o histórico de vendas: {erro}"

    if not linhas:
        return f"Nenhuma venda concluída encontrada nos últimos {dias} dias para os filtros informados."

    resultados = [f"Histórico de vendas concluídas nos últimos {dias} dias:"]

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
    semanas: int = SEMANAS_PADRAO_TENDENCIA
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
        semanas: quantidade de semanas anteriores à data atual
            consideradas na análise.
    """
    semanas = _normalizar_intervalo(semanas, SEMANAS_PADRAO_TENDENCIA, SEMANAS_MAXIMA_TENDENCIA)

    try:
        linhas = _serie_semanal_vendas(produto, categoria, semanas)

    except Exception as erro:
        return f"Não foi possível analisar a tendência de vendas: {erro}"

    if not linhas:
        return "Nenhum dado de vendas encontrado para o período e filtros informados."

    quantidades = [float(quantidade) for _, quantidade, _ in linhas]
    indices = [float(indice) for _, _, indice in linhas]

    media_semanal = statistics.mean(quantidades)
    inclinacao, _ = _regressao_linear(indices, quantidades)
    tendencia = _classificar_tendencia(inclinacao, media_semanal)

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
    semanas_historico: int = SEMANAS_PADRAO_TENDENCIA,
    semanas_previsao: int = SEMANAS_PADRAO_PREVISAO
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
        semanas_historico: quantidade de semanas de histórico
            utilizadas para calcular a tendência.
        semanas_previsao: quantidade de semanas futuras a projetar.
    """
    semanas_historico = _normalizar_intervalo(
        semanas_historico, SEMANAS_PADRAO_TENDENCIA, SEMANAS_MAXIMA_TENDENCIA
    )
    semanas_previsao = _normalizar_intervalo(
        semanas_previsao, SEMANAS_PADRAO_PREVISAO, SEMANAS_MAXIMA_PREVISAO
    )

    try:
        linhas = _serie_semanal_vendas(produto, categoria, semanas_historico)

    except Exception as erro:
        return f"Não foi possível projetar a demanda: {erro}"

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
