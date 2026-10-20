"""Configuração validada; segredos permanecem no ambiente e no .env ignorado."""
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)
    llm_timeout_seconds: float = Field(default=45, gt=0)
    llm_max_tokens: int = Field(default=2048, gt=0)
    faq_pdf_path: Path = ROOT_DIR / "data" / "FAQ_Kairos.pdf"
    faq_cache_path: Path = ROOT_DIR / ".cache" / "faq-embeddings.sqlite3"
    faq_chunk_size: int = Field(default=600, gt=0)
    faq_chunk_overlap: int = Field(default=50, ge=0)
    faq_k: int = Field(default=4, gt=0)
    faq_fetch_k: int = Field(default=10, gt=0)
    faq_lambda_mult: float = Field(default=0.7, ge=0, le=1)
    resumo_gatilho_mensagens: int = Field(default=12, ge=4)
    resumo_manter_mensagens: int = Field(default=4, ge=2)
    resumo_lote_mensagens: int = Field(default=40, ge=2)
    max_tentativas_juiz: int = Field(default=2, ge=1, le=5)
    estoque_limite_critico: float = Field(default=0.2, ge=0, le=1)
    estoque_limite_baixo: float = Field(default=0.5, ge=0, le=1)
    mongodb_database: str | None = None
    persistence_backend: Literal["mongodb", "api"] = "mongodb"
    ai_api_url: str | None = None
    ai_api_token: SecretStr | None = None
    ai_api_timeout_seconds: float = Field(default=15, gt=0, le=120)
    chatbot_service_token: SecretStr | None = None
    db_pool_min: int = Field(default=1, ge=1)
    db_pool_max: int = Field(default=10, ge=1)
    db_timeout_seconds: int = Field(default=10, ge=1)
    db_statement_timeout_ms: int = Field(default=10000, ge=1)
    limite_padrao_resultados: int = Field(default=20, ge=1)
    limite_maximo_resultados: int = Field(default=50, ge=1)
    limite_padrao_recomendacoes: int = Field(default=10, ge=1)
    historico_vendas_max_dias: int = Field(default=365, ge=1)
    historico_clientes_max_dias: int = Field(default=730, ge=1)
    tendencia_max_semanas: int = Field(default=52, ge=3)
    previsao_max_semanas: int = Field(default=12, ge=1)
    memorias_contexto_limite: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def validar_faixas(self):
        if self.faq_chunk_overlap >= self.faq_chunk_size:
            raise ValueError("FAQ_CHUNK_OVERLAP deve ser menor que FAQ_CHUNK_SIZE.")
        if self.faq_fetch_k < self.faq_k:
            raise ValueError("FAQ_FETCH_K deve ser maior ou igual a FAQ_K.")
        if self.resumo_manter_mensagens >= self.resumo_gatilho_mensagens:
            raise ValueError("A retenção do resumo deve ser menor que seu gatilho.")
        if self.estoque_limite_critico >= self.estoque_limite_baixo:
            raise ValueError("O limite crítico deve ser menor que o limite baixo.")
        if self.db_pool_min > self.db_pool_max:
            raise ValueError("DB_POOL_MIN não pode exceder DB_POOL_MAX.")
        if self.limite_padrao_resultados > self.limite_maximo_resultados:
            raise ValueError("O limite padrão de resultados não pode exceder o máximo.")
        if self.limite_padrao_recomendacoes > self.limite_maximo_resultados:
            raise ValueError("O limite padrão de recomendações não pode exceder o máximo.")
        if self.resumo_manter_mensagens % 2 or self.resumo_lote_mensagens % 2:
            raise ValueError("Retenção e lote de resumo devem preservar pares de mensagens.")
        return self


settings = Settings()
USUARIO_PADRAO_ID = 1  # Exclusivo do console de desenvolvimento.


def configurar_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _agora_dt() -> datetime:
    return datetime.now(timezone.utc)
