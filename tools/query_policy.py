"""Valida o escopo solicitado sem inventar períodos nem truncar pedidos."""
from config import settings


def validar_periodo(valor, unidade, maximo, pergunta, *, minimo=1):
    if valor is None:
        return None, pergunta + " Nenhuma consulta foi executada."
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < minimo:
        unidade_minima = unidade[:-1] if minimo == 1 else unidade
        return None, (
            f"Informe um período inteiro de pelo menos {minimo} {unidade_minima}. "
            "Nenhuma consulta foi executada."
        )
    if valor > maximo:
        return None, (
            f"Você pediu {valor} {unidade}, acima do limite atual de {maximo} {unidade}. "
            "Períodos maiores podem exigir mais processamento; o custo depende do volume de dados. "
            f"Escolha um intervalo de até {maximo} {unidade}. Nenhuma consulta foi executada."
        )
    return valor, None


def validar_limite(valor, *, padrao=None):
    if valor is None:
        valor = settings.limite_padrao_resultados if padrao is None else padrao
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 1:
        return None, "Informe uma quantidade inteira positiva de resultados. Nenhuma consulta foi executada."
    if valor > settings.limite_maximo_resultados:
        return None, (
            f"Você pediu até {valor} resultados. O limite atual por consulta é "
            f"{settings.limite_maximo_resultados}; escolha uma quantidade menor ou refine os filtros. "
            "O pedido não foi reduzido automaticamente. Nenhuma consulta foi executada."
        )
    return valor, None
