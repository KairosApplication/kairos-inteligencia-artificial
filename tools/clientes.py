import re
from typing import Optional

from config import settings

from langchain.tools import tool

from data.postgres import executar_consulta
from tools.query_policy import validar_limite, validar_periodo


# Status que representa uma compra efetivamente concluída.
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

    literal = valor.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{literal}%"


def _normalizar_cpf(cpf: Optional[str]) -> Optional[str]:
    """
    Remove qualquer caractere que não seja dígito de um CPF
    informado, permitindo comparar "123.456.789-00" com "12345678900".
    """
    if not cpf or not cpf.strip():
        return None

    apenas_digitos = re.sub(r"\D", "", cpf)

    return apenas_digitos or None


@tool
def localizar_cliente(
    nome: Optional[str] = None,
    email: Optional[str] = None,
    cpf: Optional[str] = None,
    limite: Optional[int] = None
) -> str:
    """
    Localiza clientes do Kairos pelo nome completo (ou parte dele),
    e-mail ou CPF, retornando o identificador do cliente
    (customer_id) necessário para consultar seu histórico de
    compras ou ofertas personalizadas.

    Ao menos um dos filtros (nome, email ou cpf) deve ser informado.

    Nunca retorna dados sensíveis como senha.

    Parâmetros:
        nome: nome ou parte do nome completo do cliente.
        email: e-mail (ou parte dele) do cliente.
        cpf: CPF do cliente, com ou sem pontuação.
        limite: quantidade máxima de clientes retornados.
    """
    nome_normalizado = _like(nome)
    email_normalizado = _like(email)
    cpf_normalizado = _normalizar_cpf(cpf)
    if cpf and cpf.strip() and (cpf_normalizado is None or len(cpf_normalizado) != 11):
        return "Informe um CPF com 11 dígitos."
    if not (nome_normalizado or email_normalizado or cpf_normalizado):
        return "Informe ao menos um filtro: nome, email ou cpf."

    limite, aviso = validar_limite(limite)
    if aviso:
        return aviso

    sql = """
        SELECT
            c.id AS customer_id,
            u.name,
            u.last_name,
            u.email
        FROM customers c
        JOIN users u ON u.id = c.users_id
        WHERE (%(nome)s IS NULL OR (u.name || ' ' || u.last_name) ILIKE %(nome)s)
          AND (%(email)s IS NULL OR u.email ILIKE %(email)s)
          AND (%(cpf)s IS NULL OR regexp_replace(u.cpf, '\\D', '', 'g') = %(cpf)s)
        ORDER BY u.name, u.last_name
        LIMIT %(limite)s
    """

    try:
        linhas = _executar_consulta(sql, {
            "nome": nome_normalizado,
            "email": email_normalizado,
            "cpf": cpf_normalizado,
            "limite": limite
        })

    except Exception:
        return "Não foi possível localizar o cliente. Tente novamente mais tarde."

    if not linhas:
        return "Nenhum cliente encontrado para os filtros informados."

    resultados = [f"Até {limite} clientes encontrados para os filtros informados:"]

    for customer_id, primeiro_nome, sobrenome, email_cliente in linhas:
        resultados.append(
            f"- customer_id: {customer_id} | Nome: {primeiro_nome} {sobrenome} | "
            f"E-mail: {email_cliente}"
        )

    return "\n".join(resultados)


@tool
def consultar_historico_compras_cliente(
    customer_id: int,
    dias: Optional[int] = None,
    limite: Optional[int] = None
) -> str:
    """
    Consulta o histórico de compras concluídas de um cliente
    específico do Kairos.

    Retorna, por produto comprado pelo cliente: quantidade total,
    valor total gasto, número de pedidos e a data da compra mais
    recente, considerando somente compras com status concluído.

    Use a ferramenta localizar_cliente antes para obter o
    customer_id, caso ainda não o tenha.

    Parâmetros:
        customer_id: identificador do cliente.
        dias: últimos N dias confirmados pelo usuário; ausência pede
            esclarecimento sem consultar o banco.
        limite: quantidade máxima de produtos retornados.
    """
    dias, aviso = validar_periodo(dias, "dias", settings.historico_clientes_max_dias, "De quantos dias anteriores você quer consultar as compras deste cliente?")
    if aviso:
        return aviso
    limite, aviso = validar_limite(limite)
    if aviso:
        return aviso

    sql_cliente = "SELECT 1 FROM customers WHERE id = %(customer_id)s LIMIT 1"

    sql_historico = """
        SELECT
            pr.name AS produto,
            cat.category AS categoria,
            SUM(pp.quantity) AS quantidade_total,
            SUM(pp.amount::numeric) AS valor_total,
            COUNT(DISTINCT p.id) AS numero_pedidos,
            MAX(p.date) AS ultima_compra
        FROM purchases p
        JOIN purchase_products pp ON pp.purchase_id = p.id
        JOIN products pr ON pr.id = pp.product_id
        LEFT JOIN categories cat ON cat.id = pr.category_id
        WHERE p.customer_id = %(customer_id)s
          AND p.status = %(status_concluido)s
          AND p.date >= now() - (%(dias)s * INTERVAL '1 day')
        GROUP BY pr.name, cat.category
        ORDER BY ultima_compra DESC
        LIMIT %(limite)s
    """

    try:
        cliente_encontrado = _executar_consulta(sql_cliente, {"customer_id": customer_id})

        if not cliente_encontrado:
            return f"Nenhum cliente encontrado com customer_id {customer_id}."

        linhas = _executar_consulta(sql_historico, {
            "customer_id": customer_id,
            "status_concluido": STATUS_VENDA_CONCLUIDA,
            "dias": dias,
            "limite": limite
        })

    except Exception:
        return "Não foi possível consultar o histórico do cliente. Tente novamente mais tarde."

    if not linhas:
        return f"O cliente {customer_id} não possui compras concluídas nos últimos {dias} dias."

    resultados = [f"Histórico de compras do cliente {customer_id} nos últimos {dias} dias (até {limite} produtos):"]

    for nome, categoria, quantidade, valor_total, numero_pedidos, ultima_compra in linhas:
        resultados.append(
            f"- Produto: {nome} | Categoria: {categoria or 'não especificada'} | "
            f"Quantidade: {quantidade} | Valor total: R$ {valor_total:.2f} | "
            f"Pedidos: {numero_pedidos} | Última compra: {ultima_compra}"
        )

    return "\n".join(resultados)


@tool
def sugerir_ofertas_personalizadas(
    customer_id: int,
    limite: Optional[int] = None,
    dias: Optional[int] = None
) -> str:
    """
    Sugere promoções atualmente vigentes que sejam relevantes para
    um cliente específico, com base no histórico de compras dele.

    Uma promoção é considerada relevante quando é vigente na data
    atual e o produto promovido é o mesmo que o cliente já comprou,
    ou pertence a uma categoria que o cliente já comprou.

    Esta ferramenta não cria descontos personalizados exclusivos:
    ela apenas identifica, entre as promoções vigentes cadastradas
    no sistema, quais fazem sentido para o histórico do cliente.

    Use a ferramenta localizar_cliente antes para obter o
    customer_id, caso ainda não o tenha.

    Parâmetros:
        customer_id: identificador do cliente.
        limite: quantidade máxima de ofertas retornadas.
        dias: últimos N dias de compras confirmados pelo usuário;
            se ausente, pede esclarecimento antes de recomendar ofertas.
    """
    dias, aviso = validar_periodo(
        dias, "dias", settings.historico_clientes_max_dias,
        "As ofertas devem considerar as compras dos últimos quantos dias?",
    )
    if aviso:
        return aviso
    limite, aviso = validar_limite(limite, padrao=settings.limite_padrao_recomendacoes)
    if aviso:
        return aviso

    sql_cliente = "SELECT 1 FROM customers WHERE id = %(customer_id)s LIMIT 1"

    sql_ofertas = """
        WITH produtos_cliente AS (
            SELECT DISTINCT pp.product_id
            FROM purchases p
            JOIN purchase_products pp ON pp.purchase_id = p.id
            WHERE p.customer_id = %(customer_id)s
              AND p.status = %(status_concluido)s
              AND p.date >= now() - (%(dias)s * INTERVAL '1 day')
        ),
        categorias_cliente AS (
            SELECT DISTINCT pr.category_id
            FROM produtos_cliente pc
            JOIN products pr ON pr.id = pc.product_id
            WHERE pr.category_id IS NOT NULL
        )
        SELECT
            pr.name AS produto,
            cat.category AS categoria,
            promo.discount_percentage,
            promo.description,
            promo.promotion_end_date,
            pr.price::numeric AS preco_atual,
            (promo.product_id IN (SELECT product_id FROM produtos_cliente)) AS mesmo_produto
        FROM promotions promo
        LEFT JOIN products pr ON pr.id = promo.product_id
        LEFT JOIN categories cat ON cat.id = pr.category_id
        WHERE now() BETWEEN promo.promotion_start_date AND promo.promotion_end_date
          AND (
                promo.product_id IN (SELECT product_id FROM produtos_cliente)
                OR pr.category_id IN (SELECT category_id FROM categorias_cliente)
              )
        ORDER BY mesmo_produto DESC, promo.promotion_end_date ASC
        LIMIT %(limite)s
    """

    try:
        cliente_encontrado = _executar_consulta(sql_cliente, {"customer_id": customer_id})

        if not cliente_encontrado:
            return f"Nenhum cliente encontrado com customer_id {customer_id}."

        linhas = _executar_consulta(sql_ofertas, {
            "customer_id": customer_id,
            "status_concluido": STATUS_VENDA_CONCLUIDA,
            "dias": dias,
            "limite": limite
        })

    except Exception:
        return "Não foi possível gerar as ofertas personalizadas. Tente novamente mais tarde."

    if not linhas:
        return (
            f"Nenhuma promoção vigente compatível com o histórico de "
            f"compras do cliente {customer_id} foi encontrada."
        )

    resultados = [f"Ofertas vigentes relevantes para o cliente {customer_id} (compras dos últimos {dias} dias; até {limite} ofertas):"]

    for nome, categoria, desconto, descricao, data_fim, preco_atual, mesmo_produto in linhas:
        motivo = "produto já comprado" if mesmo_produto else "categoria já comprada"

        desconto_texto = f"{desconto:.0f}%" if desconto is not None else "não especificado"
        preco_texto = f"R$ {preco_atual:.2f}" if preco_atual is not None else "não especificado"

        resultados.append(
            f"- Produto: {nome or 'não especificado'} | Categoria: {categoria or 'não especificada'} | "
            f"Desconto: {desconto_texto} | Preço atual: {preco_texto} | "
            f"Válida até: {data_fim} | Relevância: {motivo}"
            + (f" | {descricao}" if descricao else "")
        )

    return "\n".join(resultados)


CLIENTES_TOOLS = [
    localizar_cliente,
    consultar_historico_compras_cliente,
    sugerir_ofertas_personalizadas
]
