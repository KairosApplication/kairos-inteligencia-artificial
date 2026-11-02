"""Leitura limitada de indicadores Gold; o modelo nunca fornece SQL ou URLs."""
import re
import time
from datetime import date
from functools import lru_cache
from urllib.parse import urlparse

import httpx
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from config import ROOT_DIR, settings

INDICATORS = {
    "vendas_diarias": "daily_sales",
    "vendas_por_categoria": "sales_by_category",
    "estoque_por_produto": "stock_by_product",
    "rupturas": "stockout_summary",
    "reposicoes": "replenishment_summary",
    "operacoes": "operations_summary",
}
DATE_COLUMNS = ("sale_date", "sales_date", "date", "day", "data", "data_venda", "reference_date", "data_referencia")


class DatabricksUnavailable(RuntimeError):
    """Mensagem pública sem resposta bruta do provedor ou identificadores internos."""


class DatabricksSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore", case_sensitive=False, env_ignore_empty=True)
    databricks_host: str | None = None
    databricks_client_id: str | None = Field(default=None, repr=False)
    databricks_client_secret: SecretStr | None = None
    # Escopos específicos; dashboards é solicitado separadamente apenas quando utilizado.
    databricks_oauth_scopes: str = "sql unity-catalog"
    sql_warehouse_id: str | None = Field(default=None, repr=False)
    dashboard_link: str | None = Field(default=None, repr=False)
    daily_sales: str | None = Field(default=None, repr=False)
    sales_by_category: str | None = Field(default=None, repr=False)
    stock_by_product: str | None = Field(default=None, repr=False)
    stockout_summary: str | None = Field(default=None, repr=False)
    replenishment_summary: str | None = Field(default=None, repr=False)
    operations_summary: str | None = Field(default=None, repr=False)
    # Só preencher company_id se TODO o conjunto configurado pertencer a uma única empresa.
    databricks_company_id: int | None = Field(default=None, gt=0)
    databricks_company_column: str = "company_id"

    @field_validator("databricks_host")
    @classmethod
    def workspace_origin(cls, value):
        if not value:
            return None
        parsed = urlparse(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in {None, 443}
                or not parsed.hostname.endswith((".cloud.databricks.com", ".azuredatabricks.net", ".gcp.databricks.com"))):
            raise ValueError("DATABRICKS_HOST deve identificar um workspace HTTPS do Databricks.")
        # Links copiados da interface podem incluir caminho e ?o=; a API usa apenas a origem.
        return f"https://{parsed.hostname}"


def quoted_identifier(value, parts=1):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", value) or len(value.split(".")) != parts:
        raise DatabricksUnavailable("Referência de indicador não configurada corretamente no servidor.")
    return ".".join(f"`{part}`" for part in value.split("."))


class DatabricksReader:
    def __init__(self, config=None, http=None):
        self.config = config or DatabricksSettings()
        self.http = http or httpx.Client(timeout=20, trust_env=False, follow_redirects=False)
        self._tokens = {}

    def _token(self, scopes):
        config = self.config
        if not (config.databricks_host and config.databricks_client_id and config.databricks_client_secret):
            raise DatabricksUnavailable("Configure as credenciais OAuth do Databricks no servidor.")
        cached = self._tokens.get(scopes)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        try:
            response = self.http.post(config.databricks_host + "/oidc/v1/token",
                                      auth=(config.databricks_client_id, config.databricks_client_secret.get_secret_value()),
                                      data={"grant_type": "client_credentials", "scope": scopes})
            if response.status_code != 200:
                raise DatabricksUnavailable("OAuth do Databricks recusado; confira os escopos específicos e a validade da credencial.")
            payload = response.json()
            token = payload["access_token"]
            self._tokens[scopes] = (token, time.monotonic() + max(0, int(payload.get("expires_in", 3600)) - 60))
            return token
        except (httpx.HTTPError, ValueError, KeyError):
            raise DatabricksUnavailable("Não foi possível autenticar no Databricks.") from None

    def _request(self, method, path, *, scopes=None, **kwargs):
        scopes = scopes or self.config.databricks_oauth_scopes
        for attempt in range(2):
            token = self._token(scopes)
            try:
                response = self.http.request(method, self.config.databricks_host + path,
                                             headers={"Authorization": f"Bearer {token}"}, **kwargs)
            except httpx.HTTPError:
                raise DatabricksUnavailable("Databricks temporariamente indisponível.") from None
            if response.status_code == 401 and attempt == 0:
                self._tokens.pop(scopes, None)
                continue
            if response.status_code in {401, 403}:
                raise DatabricksUnavailable("Sem autorização para este recurso do Databricks. Solicite a revisão dos escopos e privilégios de leitura.")
            if response.status_code == 429:
                raise DatabricksUnavailable("Limite temporário do Databricks; tente novamente mais tarde.")
            if response.status_code != 200:
                raise DatabricksUnavailable("O recurso do Databricks não está disponível para consulta.")
            try:
                return response.json()
            except ValueError:
                raise DatabricksUnavailable("Resposta inválida do Databricks.") from None

    def indicator(self, indicator, company_id, start=None, end=None, limit=20):
        if indicator not in INDICATORS or type(company_id) is not int or company_id <= 0:
            raise DatabricksUnavailable("Indicador ou contexto de empresa inválido.")
        if type(limit) is not int or not 1 <= limit <= settings.limite_maximo_resultados:
            raise DatabricksUnavailable(f"Informe um limite entre 1 e {settings.limite_maximo_resultados} resultados.")
        try:
            start = date.fromisoformat(start) if start else None
            end = date.fromisoformat(end) if end else None
        except (ValueError, TypeError):
            raise DatabricksUnavailable("Informe datas no formato AAAA-MM-DD.") from None
        if bool(start) != bool(end) or (indicator.startswith("vendas_") and not start):
            raise DatabricksUnavailable("Informe as datas inicial e final do período desejado.")
        if start and (end < start or (end - start).days + 1 > settings.historico_vendas_max_dias):
            raise DatabricksUnavailable(f"Use um período de até {settings.historico_vendas_max_dias} dias ou divida a análise em períodos menores.")
        config = self.config
        if config.databricks_company_id is not None and config.databricks_company_id != company_id:
            raise DatabricksUnavailable("Este conjunto de indicadores não está autorizado para sua empresa.")
        table = getattr(config, INDICATORS[indicator])
        quoted = quoted_identifier(table, parts=3)
        if not config.sql_warehouse_id or not re.fullmatch(r"[A-Za-z0-9-]+", config.sql_warehouse_id):
            raise DatabricksUnavailable("Warehouse de consultas não configurado no servidor.")
        # A leitura do contrato é feita antes de qualquer SQL; falhas de permissão não iniciam o warehouse.
        metadata = self._request("GET", "/api/2.1/unity-catalog/tables/" + table)
        columns = {column["name"] for column in metadata.get("columns", [])}
        predicates, parameters = [], []
        company_column = config.databricks_company_column
        if company_column in columns:
            predicates.append(quoted_identifier(company_column) + " = :company")
            parameters.append({"name": "company", "type": "BIGINT", "value": str(company_id)})
        elif config.databricks_company_id is None:
            raise DatabricksUnavailable("Indicador sem escopo de empresa. Configure a coluna de empresa ou a vinculação exclusiva no servidor antes de consultar.")
        date_column = next((column for column in DATE_COLUMNS if column in columns), None)
        if start:
            if date_column is None:
                raise DatabricksUnavailable("Este indicador não possui uma coluna de data reconhecida para filtrar o período; ajuste o contrato antes de consultar.")
            predicates.append(f"CAST({quoted_identifier(date_column)} AS DATE) BETWEEN :start AND :end")
            parameters.extend([{"name": "start", "type": "DATE", "value": start.isoformat()},
                               {"name": "end", "type": "DATE", "value": end.isoformat()}])
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        order = f" ORDER BY {quoted_identifier(date_column)} DESC" if date_column else ""
        result = self._request("POST", "/api/2.0/sql/statements", json={
            "warehouse_id": config.sql_warehouse_id, "statement": f"SELECT * FROM {quoted}{where}{order} LIMIT {limit}",
            "parameters": parameters, "disposition": "INLINE", "format": "JSON_ARRAY", "row_limit": limit,
            "byte_limit": 200000, "wait_timeout": "10s", "on_wait_timeout": "CANCEL",
        })
        if result.get("status", {}).get("state") != "SUCCEEDED":
            raise DatabricksUnavailable("Consulta não concluída no prazo; reduza o período ou tente novamente mais tarde.")
        names = [column["name"] for column in result.get("manifest", {}).get("schema", {}).get("columns", [])]
        rows = [dict(zip(names, row)) for row in result.get("result", {}).get("data_array", [])]
        return {"ok": True, "indicador": indicator, "fonte": "database", "periodo": {"inicio": start.isoformat(), "fim": end.isoformat()} if start else None,
                "registros": rows, "limite": limit, "amostra_limitada": True,
                "aviso": "Resultados limitados; não representam automaticamente o total do período. Sem período, dados são um retrato da fonte, sem garantia de atualização em tempo real."}

    def dashboard(self, company_id):
        # A definição do dashboard não tem filtro por empresa; exige vinculação exclusiva explícita.
        if self.config.databricks_company_id != company_id:
            raise DatabricksUnavailable("A leitura do dashboard exige vinculação exclusiva à empresa no servidor.")
        parsed = urlparse(self.config.dashboard_link or "")
        if f"{parsed.scheme}://{parsed.hostname}" != self.config.databricks_host:
            raise DatabricksUnavailable("Dashboard não configurado para o workspace autorizado.")
        match = re.search(r"/dashboards(?:v3)?/([a-fA-F0-9-]{20,})", parsed.path + "#" + parsed.fragment)
        if not match:
            raise DatabricksUnavailable("Link de dashboard não reconhecido no servidor.")
        result = self._request("GET", "/api/2.0/lakeview/dashboards/" + match[1], scopes="dashboards")
        # Não entrega SQL, nomes de datasets/tabelas, links privados ou credenciais do dashboard.
        return {"ok": True, "fonte": "database", "titulo": result.get("display_name", "Dashboard"),
                "aviso": "Somente metadados do dashboard foram lidos; nenhum gráfico foi renderizado ou interpretado visualmente."}


@lru_cache(maxsize=1)
def get_databricks():
    return DatabricksReader()
