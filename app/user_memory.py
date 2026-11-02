"""Memórias de longo prazo do usuário, com persistência HTTP ou MongoDB."""
from datetime import datetime, timezone

from bson import ObjectId

from app.access import principal
from app.integrations.ai_api import get_ai_api, via_api
from app.integrations.mongodb import get_database

NOME_USER_MEMORY = "user_memory"


def _agora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def registrar_memorias(user_id: int, memorias: list[dict]) -> None:
    """Adiciona memórias com tipo, conteudo e importancia ao usuário."""
    if not memorias:
        return

    if via_api():
        return _registrar_memorias_api(user_id, memorias)
    colecao = get_database()[NOME_USER_MEMORY]

    novas_memorias = [
        {
            "memory_id": ObjectId(),
            "type": memoria["tipo"],
            "content": memoria["conteudo"],
            "importance": memoria["importancia"],
            "created_at": _agora(),
            "updated_at": _agora()
        }
        for memoria in memorias
    ]

    colecao.update_one(
        {"user_id": user_id},
        {
            "$push": {"memories": {"$each": novas_memorias}},
            "$set": {"updated_at": _agora()},
            "$setOnInsert": {"user_id": user_id}
        },
        upsert=True
    )


def carregar_memorias(user_id: int) -> list[dict]:
    """Carrega memórias do usuário em ordem decrescente de importância."""
    if via_api():
        return _carregar_memorias_api(user_id)
    colecao = get_database()[NOME_USER_MEMORY]

    documento = colecao.find_one({"user_id": user_id})

    if not documento:
        return []

    memorias = documento.get("memories", [])
    return sorted(memorias, key=lambda m: m.get("importance", 0), reverse=True)


def _registrar_memorias_api(user_id, memorias):
    if memorias:
        identity = principal()
        get_ai_api().append_memories(user_id, [
            {"type": m["tipo"], "content": m["conteudo"], "importance": m["importancia"],
             **({"access_scope": identity.scope} if identity else {})} for m in memorias
        ])


def _carregar_memorias_api(user_id):
    # Matches the deterministic record selected by the runtime append operation.
    documents = get_ai_api().list("user-memories", userId=user_id)
    document = next(documents, {})
    identity = principal()
    memories = document.get("memories") or []
    if identity is not None:
        memories = [m for m in memories if m.get("access_scope") == identity.scope]
    return sorted(memories, key=lambda m: m.get("importance", 0), reverse=True)
