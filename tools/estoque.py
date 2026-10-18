from typing import Optional

from config import settings

from langchain.tools import tool

from data.postgres import executar_consulta


LIMITE_PADRAO_RESULTADOS = settings.limite_padrao_resultados
LIMITE_MAXIMO_RESULTADOS = settings.limite_maximo_resultados




def _executar_consulta(sql: str, params: dict) -> list:
    return executar_consulta(sql, params)


def _classificar_severidade(
    quantidade: int, percentual: Optional[float], corte_critico: float, corte_baixo: float
) -> str:
    """
    Classifica o risco de ruptura de um item com base na quantidade
    atual e no percentual de ocupação da gôndola, usando os limites
    operacionais configurados, independentes dos filtros da consulta.
    """
    if quantidade == 0:
        return "ruptura"

    if percentual is None:
        return "indefinido"

    if percentual <= corte_critico:
        return "crítico"

    if percentual <= corte_baixo:
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
            ps.product_quantity::numeric
                / NULLIF(s.maximum_capacity, 0) AS percentual_ocupacao
        FROM product_shelves ps
        JOIN products p ON p.id = ps.product_id
        JOIN shelves s ON s.id = ps.shelf_id
        JOIN aisles a ON a.id = s.aisle_id
        JOIN sectors sec ON sec.id = a.sector_id
        WHERE (%(produto)s IS NULL OR p.name ILIKE %(produto)s)
          AND (%(setor)s IS NULL OR sec.name ILIKE %(setor)s)
          AND (
              %(somente_criticos)s = FALSE
              OR ps.product_quantity = 0
              OR ps.product_quantity::numeric / NULLIF(s.maximum_capacity, 0) <= %(corte_critico)s
          )
        ORDER BY percentual_ocupacao ASC NULLS LAST
        LIMIT %(limite)s
    """

    try:
        linhas = _executar_consulta(sql, {
            "produto": _like(produto),
            "setor": _like(setor),
            "limite": limite,
            "somente_criticos": somente_criticos,
            "corte_critico": settings.estoque_limite_critico
        })

    except Exception:
        return "Não foi possível consultar os níveis de estoque. Tente novamente mais tarde."

    if not linhas:
        return "Nenhum produto encontrado para os filtros informados."

    corte_critico, corte_baixo = settings.estoque_limite_critico, settings.estoque_limite_baixo

    resultados = []

    for nome, gondola_id, setor_nome, quantidade, capacidade, percentual in linhas:
        severidade = _classificar_severidade(
            quantidade,
            float(percentual) if percentual is not None else None,
            corte_critico,
            corte_baixo
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
        FROM alerts al
        JOIN shelves s ON s.id = al.shelf_id
        JOIN aisles a ON a.id = s.aisle_id
        JOIN sectors sec ON sec.id = a.sector_id
        LEFT JOIN products p ON p.id = al.product_id
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

    except Exception:
        return "Não foi possível consultar os alertas de reposição. Tente novamente mais tarde."

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

    Rupturas precedem níveis críticos e baixos. Alertas ativos e
    menor ocupação desempatarão itens da mesma faixa de severidade.

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
            ps.product_quantity::numeric
                / NULLIF(s.maximum_capacity, 0) AS percentual_ocupacao,
            EXISTS (
                SELECT 1
                FROM alerts al
                WHERE al.shelf_id = s.id
                  AND (al.product_id = p.id OR al.product_id IS NULL)
                  AND al.resolution_date IS NULL
            ) AS alerta_ativo
        FROM product_shelves ps
        JOIN products p ON p.id = ps.product_id
        JOIN shelves s ON s.id = ps.shelf_id
        JOIN aisles a ON a.id = s.aisle_id
        JOIN sectors sec ON sec.id = a.sector_id
        WHERE (%(setor)s IS NULL OR sec.name ILIKE %(setor)s)
        ORDER BY
            CASE
                WHEN ps.product_quantity = 0 THEN 0
                WHEN ps.product_quantity::numeric / NULLIF(s.maximum_capacity, 0) <= %(corte_critico)s THEN 1
                WHEN ps.product_quantity::numeric / NULLIF(s.maximum_capacity, 0) <= %(corte_baixo)s THEN 2
                ELSE 3
            END,
            alerta_ativo DESC, percentual_ocupacao ASC NULLS LAST
        LIMIT %(limite)s
    """

    try:
        linhas = _executar_consulta(sql, {
            "setor": _like(setor),
            "limite": limite,
            "corte_critico": settings.estoque_limite_critico,
            "corte_baixo": settings.estoque_limite_baixo
        })

    except Exception:
        return "Não foi possível gerar as recomendações de reposição. Tente novamente mais tarde."

    if not linhas:
        return "Nenhum item encontrado para os filtros informados."

    corte_critico, corte_baixo = settings.estoque_limite_critico, settings.estoque_limite_baixo

    resultados = []

    for posicao, linha in enumerate(linhas, start=1):
        nome, gondola_id, setor_nome, quantidade, capacidade, percentual, alerta_ativo = linha

        severidade = _classificar_severidade(
            quantidade,
            float(percentual) if percentual is not None else None,
            corte_critico,
            corte_baixo
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
