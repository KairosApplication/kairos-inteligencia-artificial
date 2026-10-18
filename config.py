"""Configuração validada; segredos permanecem no ambiente e no .env ignorado."""
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)
    openai_chat_model: str = "gpt-4.1-mini"
    openai_fast_model: str = "gpt-4.1-mini"
    groq_chat_model: str = "openai/gpt-oss-120b"
    groq_fast_model: str = "openai/gpt-oss-20b"
    chat_temperature: float = Field(default=0.2, ge=0, le=2)
    llm_timeout_seconds: float = Field(default=45, gt=0)
    llm_max_tokens: int = Field(default=2048, gt=0)
    openai_embedding_model: str = "text-embedding-3-small"
    faq_pdf_path: Path = ROOT_DIR / "data" / "FAQ_Kairos.pdf"
    faq_cache_path: Path = ROOT_DIR / ".cache" / "faq-embeddings.sqlite3"
    faq_chunk_size: int = Field(default=600, gt=0)
    faq_chunk_overlap: int = Field(default=50, ge=0)
    faq_k: int = Field(default=4, gt=0)
    faq_fetch_k: int = Field(default=10, gt=0)
    faq_lambda_mult: float = Field(default=0.7, ge=0, le=1)
    resumo_gatilho_mensagens: int = Field(default=12, ge=4)
    resumo_manter_mensagens: int = Field(default=4, ge=2)

    @model_validator(mode="after")
    def validar_faixas(self):
        if self.faq_chunk_overlap >= self.faq_chunk_size:
            raise ValueError("FAQ_CHUNK_OVERLAP deve ser menor que FAQ_CHUNK_SIZE.")
        if self.faq_fetch_k < self.faq_k:
            raise ValueError("FAQ_FETCH_K deve ser maior ou igual a FAQ_K.")
        if self.resumo_manter_mensagens >= self.resumo_gatilho_mensagens:
            raise ValueError("A retenção do resumo deve ser menor que seu gatilho.")
        return self


settings = Settings()
USUARIO_PADRAO_ID = 1  # Exclusivo do console de desenvolvimento.


def configurar_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _agora_dt() -> datetime:
    return datetime.now(timezone.utc)
