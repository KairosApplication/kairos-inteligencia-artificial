"""
Memória de longo prazo do Kairos.

Este módulo é responsável por carregar e persistir o histórico de
conversas por usuário na coleção `conversations` do banco `kairos_ai`
no MongoDB, incluindo os resumos gerados pelo SummarizationMiddleware
do LangChain (ver `data/conversation_middleware.py`).

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

# Quantidade de mensagens brutas (ainda não cobertas por um resumo)
# recarregadas do histórico a cada novo turno, além do resumo, quando
# existir. Mantém o contexto recente disponível para o agente sem
# depender exclusivamente do resumo.
JANELA_MENSAGENS_RECENTES = 12


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
    resumo (se existir) seguido da janela de mensagens brutas mais
    recentes ainda não cobertas por um resumo.
    """
    mensagens: list[BaseMessage] = []

    resumo = conversa.get("summary")

    if resumo:
        # Reutiliza o mesmo formato de mensagem produzido pelo
        # SummarizationMiddleware, para que o agente interprete o
        # resumo exatamente como interpretaria um resumo gerado na
        # própria execução atual.
        mensagens.append(
            HumanMessage(
                content=f"Here is a summary of the conversation to date:\n\n{resumo}",
                additional_kwargs={"lc_source": "summarization"}
            )
        )

    ja_resumidas = conversa.get("summarized_count", 0)
    mensagens_brutas = conversa.get("messages", [])[ja_resumidas:]
    mensagens_brutas = mensagens_brutas[-JANELA_MENSAGENS_RECENTES:]

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

    O MongoSummarizationMiddleware utiliza esse valor para saber
    quantas mensagens marcar como "cobertas pelo resumo" caso a
    sumarização seja disparada durante o turno atual.
    """
    ja_resumidas = conversa.get("summarized_count", 0)
    total_disponivel = len(conversa.get("messages", [])) - ja_resumidas
    return max(0, min(total_disponivel, JANELA_MENSAGENS_RECENTES))


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


def registrar_resumo(conversation_id: ObjectId, resumo: str, mensagens_cobertas: int) -> None:
    """
    Persiste um novo resumo gerado pelo SummarizationMiddleware para a
    conversa informada, e avança o marcador de mensagens já cobertas
    pelo resumo, evitando que voltem a ser recarregadas como mensagens
    brutas em turnos futuros.
    """
    if mensagens_cobertas <= 0:
        # Ainda assim salva o texto do resumo, apenas não avança o
        # marcador (não há mensagens brutas novas cobertas).
        colecao = get_database()[NOME_COLECAO]
        colecao.update_one(
            {"_id": conversation_id},
            {"$set": {"summary": resumo, "summary_updated_at": _agora()}}
        )
        return

    colecao = get_database()[NOME_COLECAO]

    colecao.update_one(
        {"_id": conversation_id},
        [
            {
                "$set": {
                    "summary": resumo,
                    "summary_updated_at": _agora(),
                    "summarized_count": {
                        "$min": [
                            {"$add": [{"$ifNull": ["$summarized_count", 0]}, mensagens_cobertas]},
                            {"$size": {"$ifNull": ["$messages", []]}}
                        ]
                    }
                }
            }
        ]
    )
