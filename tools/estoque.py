from typing import Optional

from dotenv import load_dotenv
from langchain.tools import tool

from data.postgres import get_connection, release_connection

load_dotenv()

# Faixas de severidade utilizadas para classificar o risco de ruptura
# de um produto em uma gôndola, com base no percentual de ocupação:
#
#   percentual_ocupacao = quantidade_atual / capacidade_maxima_da_gondola
#
# quantidade_atual == 0            -> "ruptura"
# percentual <= LIMITE_CRITICO     -> "crítico"
# percentual <= LIMITE_BAIXO       -> "baixo"
# percentual > LIMITE_BAIXO        -> "adequado"
LIMITE_CRITICO = 0.2
LIMITE_BAIXO = 0.5

LIMITE_PADRAO_RESULTADOS = 20
LIMITE_MAXIMO_RESULTADOS = 50


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


def _classificar_severidade(quantidade: int, percentual: Optional[float]) -> str:
    """
    Classifica o risco de ruptura de um item com base na quantidade
    atual e no percentual de ocupação da gôndola.
    """
    if quantidade == 0:
        return "ruptura"

    if percentual is None:
        return "indefinido"

    if percentual <= LIMITE_CRITICO:
        return "crítico"

    if percentual <= LIMITE_BAIXO:
        return "baixo"

    return "adequado"


def _normalizar_limite(limite: int) -> int:
    """
    Garante que o limite de resultados solicitado esteja dentro
    de uma faixa segura, evitando consultas sem limite definido.
    """
    if not limite or limite <= 0:
        return LIMITE_PADRAO_RESULTADOS

    return min(limite, LIMITE_MAXIMO_RESULTADOS)


def _like(valor: Optional[str]) -> Optional[str]:
    """
    Prepara um valor de texto para busca parcial com ILIKE.
    Retorna None quando o valor não foi informado.
    """
    if not valor or not valor.strip():
        return None

    return f"%{valor.strip()}%"


def _formatar_percentual(percentual) -> str:
    """
    Formata o percentual de ocupação retornado pelo banco (Decimal)
    como texto, tratando o caso em que não foi possível calculá-lo.
    """
    if percentual is None:
        return "indefinido"

    return f"{percentual * 100:.0f}%"


@tool
def consultar_niveis_estoque(
    produto: Optional[str] = None,
    setor: Optional[str] = None,
    somente_criticos: bool = False,
    limite: int = LIMITE_PADRAO_RESULTADOS
) -> str:
    """
    Consulta o nível de estoque de produtos nas gôndolas do Kairos.

    Retorna, para cada produto/gôndola encontrado: quantidade atual,
    capacidade máxima da gôndola, percentual de ocupação e a
    severidade do risco de ruptura (ruptura, crítico, baixo ou
    adequado).

    Parâmetros:
        produto: filtra pelo nome (ou parte do nome) do produto.
        setor: filtra pelo nome (ou parte do nome) do setor da loja.
        somente_criticos: quando verdadeiro, retorna apenas itens
            classificados como "crítico" ou "ruptura".
        limite: quantidade máxima de resultados retornados.
    """
    limite = _normalizar_limite(limite)

    sql = """
        SELECT
            p.name AS produto,
            s.id AS gondola_id,
            sec.name AS setor,
            ps.product_quantity AS quantidade_atual,
            s.maximum_capacity AS capacidade_maxima,
            ROUND(
                ps.product_quantity::numeric
                / NULLIF(s.maximum_capacity, 0),
                2
            ) AS percentual_ocupacao
        FROM product_shelf ps
        JOIN product p ON p.id = ps.product_id
        JOIN shelf s ON s.id = ps.shelf_id
        JOIN aisle a ON a.id = s.aisle_id
        JOIN sector sec ON sec.id = a.sector_id
        WHERE (%(produto)s IS NULL OR p.name ILIKE %(produto)s)
          AND (%(setor)s IS NULL OR sec.name ILIKE %(setor)s)
        ORDER BY percentual_ocupacao ASC NULLS LAST
        LIMIT %(limite)s
    """

    try:
        linhas = _executar_consulta(sql, {
            "produto": _like(produto),
            "setor": _like(setor),
            "limite": limite
        })

    except Exception as erro:
        return f"Não foi possível consultar os níveis de estoque: {erro}"

    if not linhas:
        return "Nenhum produto encontrado para os filtros informados."

    resultados = []

    for nome, gondola_id, setor_nome, quantidade, capacidade, percentual in linhas:
        severidade = _classificar_severidade(
            quantidade,
            float(percentual) if percentual is not None else None
        )

        if somente_criticos and severidade not in ("crítico", "ruptura"):
            continue

        resultados.append(
            f"- Produto: {nome} | Gôndola: {gondola_id} | Setor: {setor_nome} | "
            f"Quantidade atual: {quantidade} | Capacidade máxima: {capacidade} | "
            f"Ocupação: {_formatar_percentual(percentual)} | Severidade: {severidade}"
        )

    if not resultados:
        return "Nenhum produto crítico encontrado para os filtros informados."

    return "\n".join(resultados)


@tool
def listar_alertas_reposicao(
    setor: Optional[str] = None,
    apenas_pendentes: bool = True,
    limite: int = LIMITE_PADRAO_RESULTADOS
) -> str:
    """
    Lista os alertas de ruptura de estoque registrados no sistema.

    Um alerta é considerado pendente quando ainda não possui data
    de resolução registrada.

    Parâmetros:
        setor: filtra pelo nome (ou parte do nome) do setor da loja.
        apenas_pendentes: quando verdadeiro (padrão), retorna somente
            alertas que ainda não foram resolvidos.
        limite: quantidade máxima de resultados retornados.
    """
    limite = _normalizar_limite(limite)

    sql = """
        SELECT
            al.id,
            al.description,
            al.status,
            al.stockout_date,
            al.resolution_date,
            p.name AS produto,
            s.id AS gondola_id,
            sec.name AS setor
        FROM alert al
        JOIN shelf s ON s.id = al.shelf_id
        JOIN aisle a ON a.id = s.aisle_id
        JOIN sector sec ON sec.id = a.sector_id
        LEFT JOIN product p ON p.id = al.product_id
        WHERE (%(pendentes)s = FALSE OR al.resolution_date IS NULL)
          AND (%(setor)s IS NULL OR sec.name ILIKE %(setor)s)
        ORDER BY al.stockout_date DESC NULLS LAST
        LIMIT %(limite)s
    """

    try:
        linhas = _executar_consulta(sql, {
            "pendentes": apenas_pendentes,
            "setor": _like(setor),
            "limite": limite
        })

    except Exception as erro:
        return f"Não foi possível consultar os alertas de reposição: {erro}"

    if not linhas:
        return "Nenhum alerta encontrado para os filtros informados."

    resultados = []

    for alerta_id, descricao, status, data_ruptura, data_resolucao, produto, gondola_id, setor_nome in linhas:
        situacao = "resolvido" if data_resolucao else "pendente"

        resultados.append(
            f"- Alerta #{alerta_id} | Produto: {produto or 'não especificado'} | "
            f"Gôndola: {gondola_id} | Setor: {setor_nome} | "
            f"Status: {status} | Situação: {situacao} | "
            f"Data da ruptura: {data_ruptura} | Descrição: {descricao}"
        )

    return "\n".join(resultados)


@tool
def recomendar_prioridades_reposicao(
    setor: Optional[str] = None,
    limite: int = 10
) -> str:
    """
    Recomenda prioridades de reposição com base no nível de estoque
    e na existência de alertas de ruptura ativos.

    Itens com alerta ativo são priorizados. Entre os demais, a
    prioridade segue o menor percentual de ocupação da gôndola.

    Parâmetros:
        setor: filtra pelo nome (ou parte do nome) do setor da loja.
        limite: quantidade máxima de recomendações retornadas.
    """
    limite = _normalizar_limite(limite)

    sql = """
        SELECT
            p.name AS produto,
            s.id AS gondola_id,
            sec.name AS setor,
            ps.product_quantity AS quantidade_atual,
            s.maximum_capacity AS capacidade_maxima,
            ROUND(
                ps.product_quantity::numeric
                / NULLIF(s.maximum_capacity, 0),
                2
            ) AS percentual_ocupacao,
            EXISTS (
                SELECT 1
                FROM alert al
                WHERE al.shelf_id = s.id
                  AND (al.product_id = p.id OR al.product_id IS NULL)
                  AND al.resolution_date IS NULL
            ) AS alerta_ativo
        FROM product_shelf ps
        JOIN product p ON p.id = ps.product_id
        JOIN shelf s ON s.id = ps.shelf_id
        JOIN aisle a ON a.id = s.aisle_id
        JOIN sector sec ON sec.id = a.sector_id
        WHERE (%(setor)s IS NULL OR sec.name ILIKE %(setor)s)
        ORDER BY alerta_ativo DESC, percentual_ocupacao ASC NULLS LAST
        LIMIT %(limite)s
    """

    try:
        linhas = _executar_consulta(sql, {
            "setor": _like(setor),
            "limite": limite
        })

    except Exception as erro:
        return f"Não foi possível gerar as recomendações de reposição: {erro}"

    if not linhas:
        return "Nenhum item encontrado para os filtros informados."

    resultados = []

    for posicao, linha in enumerate(linhas, start=1):
        nome, gondola_id, setor_nome, quantidade, capacidade, percentual, alerta_ativo = linha

        severidade = _classificar_severidade(
            quantidade,
            float(percentual) if percentual is not None else None
        )

        alerta_texto = " | Alerta ativo" if alerta_ativo else ""

        resultados.append(
            f"{posicao}. Produto: {nome} | Gôndola: {gondola_id} | Setor: {setor_nome} | "
            f"Quantidade atual: {quantidade} | Ocupação: {_formatar_percentual(percentual)} | "
            f"Severidade: {severidade}{alerta_texto}"
        )

    return "\n".join(resultados)


ESTOQUE_TOOLS = [
    consultar_niveis_estoque,
    listar_alertas_reposicao,
    recomendar_prioridades_reposicao
]
