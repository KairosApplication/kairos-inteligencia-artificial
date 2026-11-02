"""MCP privado, iniciado e encerrado pelo ciclo de vida da aplicação."""
from contextlib import asynccontextmanager
from typing import Any, Literal

import jwt
from anyio.lowlevel import current_token
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from app.auth import verify_analyst_token
from app.integrations.databricks import DatabricksUnavailable, get_databricks

server = MCPServer("Kairos Indicadores", log_level="WARNING")
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)


def trusted_company(ctx):
    token = (ctx.request_context.meta or {}).get("kairos_identity")
    if not isinstance(token, str):
        raise DatabricksUnavailable("Identidade não fornecida pelo servidor do analista.")
    try:
        return verify_analyst_token(token).company_id
    except (jwt.InvalidTokenError, PermissionError, RuntimeError, ValueError, TypeError, KeyError):
        raise DatabricksUnavailable("Identidade não autorizada para consultar os indicadores.") from None


@server.tool(annotations=READ_ONLY, structured_output=True)
def consultar_indicador(
    indicador: Literal["vendas_diarias", "vendas_por_categoria", "estoque_por_produto", "rupturas", "reposicoes", "operacoes"],
    ctx: Context,
    data_inicio: str | None = None, data_fim: str | None = None, limite: int = 20,
) -> dict[str, Any]:
    """Lê um indicador Gold autorizado. Vendas exigem período explícito; não aceita SQL ou empresa do modelo."""
    try:
        company_id = trusted_company(ctx)
        return get_databricks().indicator(indicador, company_id, data_inicio, data_fim, limite)
    except DatabricksUnavailable as error:
        return {"ok": False, "erro": str(error)}


@server.tool(annotations=READ_ONLY, structured_output=True)
def consultar_dashboard(ctx: Context) -> dict[str, Any]:
    """Lê somente metadados do dashboard configurado, sem renderizar gráficos nem executar seu SQL."""
    try:
        company_id = trusted_company(ctx)
        return get_databricks().dashboard(company_id)
    except DatabricksUnavailable as error:
        return {"ok": False, "erro": str(error)}


@asynccontextmanager
async def analyst_lifespan(app):
    async with Client(server, read_timeout_seconds=35) as client:
        await client.list_tools()
        app.state.analyst_mcp = client
        app.state.analyst_loop_token = current_token()
        try:
            yield
        finally:
            app.state.analyst_mcp = None
            app.state.analyst_loop_token = None
