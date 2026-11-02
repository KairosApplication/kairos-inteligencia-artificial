"""Persiste conversas e carrega resumos e turnos ainda não compactados no escopo do usuário."""

from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from data.mongodb import get_database
from data.ai_api import get_ai_api, via_api, PersistenceError
from data.request_context import chat_context
from data.access import principal
from data.answer_sources import public_sources

NOME_COLECAO = "conversations"

def _agora() -> str:
    """Retorna o instante atual em UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def obter_ou_criar_conversa_ativa(user_id: int) -> dict:
    """Obtém a conversa ativa do usuário ou cria uma nova."""
    if via_api():
        return get_ai_api().active_conversation(user_id)
    colecao = get_database()[NOME_COLECAO]

    conversa = colecao.find_one(
        {"user_id": user_id, "status": "ACTIVE"},
        sort=[("updated_at", -1)]
    )

    if conversa:
        return conversa

    novo_documento = {
        "user_id": user_id,
        "status": "ACTIVE",
        "created_at": _agora(),
        "updated_at": _agora(),
        "messages": [],
        "summary": None,
        "summary_updated_at": None,
        # Quantidade de mensagens (a partir do início da lista) já
        # incorporadas a um resumo, e que portanto não precisam ser
        # recarregadas como mensagens brutas em turnos futuros.
        "summarized_count": 0
    }

    resultado = colecao.insert_one(novo_documento)
    novo_documento["_id"] = resultado.inserted_id

    return novo_documento


def obter_conversa_por_id(conversation_id: ObjectId) -> Optional[dict]:
    """Recarrega a conversa com os resumos e marcadores atualizados."""
    if via_api():
        context = chat_context.get()
        if context is not None and context.conversation_id != conversation_id:
            raise PermissionError("Conversa diferente do contexto autorizado.")
        try:
            document = (get_ai_api().conversation(context.user_id, conversation_id) if context is not None
                        else get_ai_api().get("conversations", conversation_id))
            identity = principal()
            if identity is not None and (document.get("access_scope") != identity.scope
                                         or document.get("user_id") != identity.user_id):
                raise PermissionError("Conversa fora do escopo de acesso atual.")
            return document
        except PersistenceError as error:
            if error.status == 404:
                return None
            raise
    colecao = get_database()[NOME_COLECAO]
    return colecao.find_one({"_id": conversation_id})


def carregar_mensagens_contexto(conversa: dict) -> list[BaseMessage]:
    """Monta o contexto com resumo e mensagens ainda não resumidas, após a compactação."""
    mensagens: list[BaseMessage] = []

    resumo = conversa.get("summary")

    if resumo:
        # O resumo anterior fornece contexto; não comprova dados operacionais atuais.
        mensagens.append(
            HumanMessage(
                content=f"Here is a summary of the conversation to date:\n\n{resumo}",
                additional_kwargs={"lc_source": "summarization"}
            )
        )

    ja_resumidas = conversa.get("summarized_count", 0)
    mensagens_brutas = conversa.get("messages", [])[ja_resumidas:]

    for mensagem in mensagens_brutas:
        if mensagem["sender"] == "USER":
            mensagens.append(HumanMessage(content=mensagem["content"]))
        else:
            mensagens.append(AIMessage(content=mensagem["content"]))

    return mensagens


def contar_mensagens_brutas_carregadas(conversa: dict) -> int:
    """Conta as mensagens brutas do contexto, incluindo as pendentes de compactação."""
    ja_resumidas = conversa.get("summarized_count", 0)
    total_disponivel = len(conversa.get("messages", [])) - ja_resumidas
    return max(0, total_disponivel)


def registrar_turno(conversation_id: ObjectId, pergunta: str, resposta: str, *, sources=None) -> None:
    """Persiste o par pergunta/resposta final e atualiza a atividade da conversa."""
    if via_api():
        context = chat_context.get()
        conversa = obter_conversa_por_id(conversation_id)
        if conversa is None:
            raise RuntimeError("Conversa não encontrada.")
        get_ai_api().append_turn(conversa["user_id"], conversation_id, pergunta, resposta,
                                 context.request_id if context is not None else str(ObjectId()),
                                 **({"sources": public_sources(sources)} if sources is not None else {}))
        return
    colecao = get_database()[NOME_COLECAO]

    novas_mensagens = [
        {
            "message_id": str(ObjectId()),
            "sender": "USER",
            "content": pergunta,
            "timestamp": _agora()
        },
        {
            "message_id": str(ObjectId()),
            "sender": "AI",
            "content": resposta,
            "sources": public_sources(sources),
            "timestamp": _agora()
        }
    ]

    colecao.update_one(
        {"_id": conversation_id},
        {
            "$push": {"messages": {"$each": novas_mensagens}},
            "$set": {"updated_at": _agora()}
        }
    )


def registrar_resumo(
    conversation_id: ObjectId, resumo: str, marcador_anterior: int,
    marcador_novo: int, *, resumo_anterior: Optional[str] = None
) -> bool:
    """Troca o resumo somente se a versão lida ainda é a atual."""
    if marcador_anterior < 0 or marcador_novo <= marcador_anterior:
        raise ValueError("Marcadores de resumo inválidos.")
    if via_api():
        conversa = obter_conversa_por_id(conversation_id)
        if conversa is None:
            raise RuntimeError("Conversa não encontrada.")
        return get_ai_api().summarize(conversa["user_id"], conversation_id, resumo,
                                      marcador_anterior, marcador_novo, resumo_anterior)
    resultado = get_database()[NOME_COLECAO].update_one(
        {
            "_id": conversation_id,
            "summary": resumo_anterior,
            "$expr": {"$and": [
                {"$eq": [{"$ifNull": ["$summarized_count", 0]}, marcador_anterior]},
                {"$gte": [{"$size": {"$ifNull": ["$messages", []]}}, marcador_novo]}
            ]}
        },
        {"$set": {
            "summary": resumo,
            "summary_updated_at": _agora(),
            "summarized_count": marcador_novo
        }}
    )
    return resultado.modified_count == 1
