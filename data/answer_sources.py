"""Rótulos públicos derivados de consultas executadas, sem metadados internos."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

AnswerSource = Literal["pdf", "database", "memory"]
SOURCE_TYPES = ("pdf", "database", "memory")
_consulted: ContextVar[set[str] | None] = ContextVar("answer_sources", default=None)


def public_sources(value) -> list[AnswerSource]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [source for source in SOURCE_TYPES if source in value]


def consulted(source: AnswerSource):
    sources = _consulted.get()
    if sources is not None and source in SOURCE_TYPES:
        sources.add(source)


@contextmanager
def capture_sources():
    sources = set()
    token = _consulted.set(sources)
    try:
        yield sources
    finally:
        _consulted.reset(token)
