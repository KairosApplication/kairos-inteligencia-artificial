"""
Memória de longo prazo do Kairos.

Este módulo é responsável por carregar e persistir o histórico de
conversas por usuário na coleção `conversations` do banco `kairos_ai`
no MongoDB, incluindo os resumos produzidos por compactação de turnos aprovados
(ver `data/conversation_middleware.py`).

Como o histórico é armazenado no MongoDB (e não apenas em memória do
processo Python), a conversa é retomada mesmo depois que o chat de
teste é encerrado e executado novamente, o que caracteriza memória de
longo prazo.
"""

from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from data.mongodb import get_database

NOME_COLECAO = "conversations"

def _agora() -> str:
    """
    Retorna o timestamp atual em UTC, no mesmo formato de string já
    utilizado pelos documentos existentes na coleção.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def obter_ou_criar_conversa_ativa(user_id: int) -> dict:
    """
    Obtém a conversa com status ACTIVE mais recente do usuário,
    criando uma nova (vazia) caso não exista nenhuma.
    """
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
    """
    Recarrega o documento da conversa a partir do MongoDB. Utilizado
    a cada novo turno para obter o resumo/marcadores mais recentes,
    já que podem ter sido atualizados pelo middleware de sumarização
    no turno anterior.
    """
    colecao = get_database()[NOME_COLECAO]
    return colecao.find_one({"_id": conversation_id})


def carregar_mensagens_contexto(conversa: dict) -> list[BaseMessage]:
    """
    Reconstrói, a partir da conversa armazenada, a lista de mensagens
    LangChain utilizada como contexto inicial de um novo turno: o
    resumo (se existir) seguido de todas as mensagens brutas ainda
    não cobertas por um resumo. A compactação ocorre antes desta leitura.
    """
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
    """
    Retorna quantas mensagens brutas foram efetivamente incluídas por
    `carregar_mensagens_contexto` para a conversa informada.

    A contagem não descarta mensagens pendentes de compactação.
    """
    ja_resumidas = conversa.get("summarized_count", 0)
    total_disponivel = len(conversa.get("messages", [])) - ja_resumidas
    return max(0, total_disponivel)


def registrar_turno(conversation_id: ObjectId, pergunta: str, resposta: str) -> None:
    """
    Adiciona a pergunta do usuário e a resposta final do agente ao
    histórico bruto da conversa, e atualiza o horário da última
    atividade.
    """
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
