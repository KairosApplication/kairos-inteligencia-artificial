"""Servidor MCP privado por stdio. Iniciar com python -m data.analyst_mcp."""
import os
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from data.databricks import DatabricksUnavailable, get_databricks

server = MCPServer("Kairos Indicadores", log_level="WARNING")
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)


def trusted_company():
    value = os.environ.get("KAIROS_ANALYST_COMPANY_ID", "")
    if not value.isdigit() or int(value) <= 0:
        raise DatabricksUnavailable("Contexto de empresa não fornecido pelo servidor do analista.")
    return int(value)


@server.tool(annotations=READ_ONLY, structured_output=True)
def consultar_indicador(
    indicador: Literal["vendas_diarias", "vendas_por_categoria", "estoque_por_produto", "rupturas", "reposicoes", "operacoes"],
    data_inicio: str | None = None, data_fim: str | None = None, limite: int = 20,
) -> dict[str, Any]:
    """Lê um indicador Gold autorizado. Vendas exigem período explícito; não aceita SQL ou empresa do modelo."""
    try:
        return get_databricks().indicator(indicador, trusted_company(), data_inicio, data_fim, limite)
    except DatabricksUnavailable as error:
        return {"ok": False, "erro": str(error)}


@server.tool(annotations=READ_ONLY, structured_output=True)
def consultar_dashboard() -> dict[str, Any]:
    """Lê somente metadados do dashboard configurado, sem renderizar gráficos nem executar seu SQL."""
    try:
        return get_databricks().dashboard(trusted_company())
    except DatabricksUnavailable as error:
        return {"ok": False, "erro": str(error)}


if __name__ == "__main__":
    server.run(transport="stdio")
