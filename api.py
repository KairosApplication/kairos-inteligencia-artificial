"""Chatbot FastAPI: rotas internas e rotas pessoais com identidade verificada."""
import logging
import secrets
from contextlib import contextmanager
from threading import Event, Thread
from uuid import uuid4

from bson import ObjectId
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from config import settings
from data.ai_api import PersistenceError, get_ai_api, via_api
from data.request_context import ChatContext, chat_context
from data.access import Principal
from data.auth import authenticated_user
from data.answer_sources import AnswerSource, public_sources

logger = logging.getLogger(__name__)
app = FastAPI(title="Kairos Chatbot", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=settings.chat_allowed_origins, allow_credentials=True,
                   allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type", "X-CSRF-TOKEN"])
bearer = HTTPBearer(auto_error=False)


@app.middleware("http")
async def preserve_auth_cookies(request, call_next):
    response = await call_next(request)
    for cookie in getattr(request.state, "auth_set_cookies", []):
        response.headers.append("set-cookie", cookie)
    if request.url.path.startswith("/api/v1/me/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def service_access(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    token = settings.chatbot_service_token
    if token is None or len(token.get_secret_value()) < 32:
        raise HTTPException(503, "Acesso interno não configurado.")
    if credentials is None or not secrets.compare_digest(credentials.credentials.encode(), token.get_secret_value().encode()):
        raise HTTPException(401, "Token de serviço inválido.", headers={"WWW-Authenticate": "Bearer"})
    if not via_api():
        raise HTTPException(503, "A API do chatbot exige a persistência HTTP configurada.")
    try:
        get_ai_api()
    except (RuntimeError, ValueError):
        raise HTTPException(503, "Conexão com a API de IA não configurada.") from None


def user_persistence():
    if not via_api():
        raise HTTPException(503, "Configure a persistência HTTP antes de habilitar o chat autenticado.")
    try:
        get_ai_api()
    except (RuntimeError, ValueError):
        raise HTTPException(503, "Persistência HTTP não configurada.") from None


class UserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    user_id: int = Field(alias="userId", gt=0, le=2147483647, strict=True)


class ChatRequest(UserRequest):
    question: str = Field(min_length=1, max_length=10000, pattern=r"\S")
    conversation_id: str | None = Field(default=None, alias="conversationId", pattern=r"^[a-fA-F0-9]{24}$")


class PersonalChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    question: str = Field(min_length=1, max_length=10000, pattern=r"\S")
    conversation_id: str | None = Field(default=None, alias="conversationId", pattern=r"^[a-fA-F0-9]{24}$")


class ChatResponse(BaseModel):
    answer: str
    conversationId: str
    executionId: str
    blocked: bool
    approved: bool
    sources: list[AnswerSource] = Field(default_factory=list)


def execute_graph(state):
    # Importação lazy: health, validação e autenticação não inicializam agentes.
    from graph import grafo
    return grafo.invoke(state)


@contextmanager
def conversation_lease(api, user_id, conversation_id, owner):
    api.lease(user_id, conversation_id, owner)
    stop = Event()
    lost = Event()

    def renew():
        while not stop.wait(10):
            try:
                api.lease(user_id, conversation_id, owner, action="renew")
            except PersistenceError:
                lost.set()
                return

    worker = Thread(target=renew, daemon=True)
    worker.start()
    try:
        yield lost
    finally:
        stop.set()
        worker.join(timeout=settings.ai_api_timeout_seconds + 1)
        try:
            api.lease(user_id, conversation_id, owner, action="release")
        except PersistenceError:
            logger.warning("Não foi possível liberar a reserva da conversa; ela expirará no servidor.")


@app.get("/health")
def health():
    return {"status": "UP"}


@app.exception_handler(PersistenceError)
def persistence_failure(request, error):
    status = error.status if error.status in {404, 409} else 503
    return JSONResponse(status_code=status, content={"detail": "Recurso indisponível para este usuário ou operação em conflito."})


@app.post("/api/v1/chat/conversations", dependencies=[Depends(service_access)], status_code=201)
def create_conversation(request: UserRequest):
    document = get_ai_api().create("conversations", {"user_id": request.user_id, "status": "ACTIVE", "summarized_count": 0})
    return {"id": str(document["_id"]), "userId": request.user_id, "status": document["status"]}


@app.get("/api/v1/chat/users/{user_id}/conversations", dependencies=[Depends(service_access)])
def list_conversations(user_id: int, page: int = Query(default=0, ge=0, le=10000), size: int = Query(default=20, ge=1, le=100)):
    if user_id <= 0 or user_id > 2147483647:
        raise HTTPException(422, "Identificador de usuário inválido.")
    result = get_ai_api().request("GET", "conversations", params={"userId": user_id, "page": page, "size": size})
    # A lista não precisa transportar todo o histórico de cada conversa.
    result["items"] = [{key: item.get(key) for key in ("id", "status", "createdAt", "updatedAt")}
                       for item in result["items"]]
    return result


@app.get("/api/v1/chat/users/{user_id}/conversations/{conversation_id}", dependencies=[Depends(service_access)])
def read_conversation(user_id: int, conversation_id: str):
    if user_id <= 0 or user_id > 2147483647 or not ObjectId.is_valid(conversation_id):
        raise HTTPException(422, "Identificador inválido.")
    return get_ai_api().request("GET", f"runtime/users/{user_id}/conversations/{conversation_id}")


@app.post("/api/v1/chat/messages", response_model=ChatResponse, dependencies=[Depends(service_access)])
def message(request: ChatRequest):
    return process_message(request)


def process_message(request: ChatRequest, identity: Principal | None = None):
    try:
        api = get_ai_api()
        if identity is not None:
            conversation = (authorized_conversation(identity, request.conversation_id) if request.conversation_id
                            else active_user_conversation(identity))
        else:
            conversation = (api.conversation(request.user_id, request.conversation_id) if request.conversation_id
                            else api.active_conversation(request.user_id))
        conversation_id = conversation["_id"]
        owner = str(uuid4())
        with conversation_lease(api, request.user_id, conversation_id, owner) as lost:
            context_token = chat_context.set(ChatContext(request.user_id, conversation_id, owner, identity))
            try:
                result = execute_graph({"pergunta": request.question, "user_id": request.user_id,
                                        "conversation_id": conversation_id})
                if lost.is_set():
                    raise PersistenceError(409)
            finally:
                chat_context.reset(context_token)
        return ChatResponse(answer=result["resposta"], conversationId=str(conversation_id),
                            executionId=str(result["execution_id"]), blocked=result.get("bloqueado", False),
                            approved=result.get("aprovado", False),
                            sources=(public_sources(result.get("sources"))
                                     if result.get("aprovado", False) and not result.get("bloqueado", False) else []))
    except HTTPException:
        raise
    except PermissionError:
        raise HTTPException(403, "Seu perfil não permite acessar estes dados.") from None
    except PersistenceError as error:
        status = error.status if error.status in {404, 409} else 503
        detail = {404: "Conversa não encontrada para este usuário.", 409: "A conversa está ocupada ou foi alterada. Tente novamente."}.get(status, "Persistência temporariamente indisponível.")
        raise HTTPException(status, detail) from None
    except Exception:
        logger.warning("Não foi possível concluir a solicitação do chatbot.")
        raise HTTPException(503, "Não foi possível concluir a solicitação.") from None


def authorized_conversation(identity, conversation_id):
    if not ObjectId.is_valid(conversation_id):
        raise HTTPException(422, "Identificador inválido.")
    document = get_ai_api().conversation(identity.user_id, conversation_id)
    if document.get("user_id") != identity.user_id or document.get("access_scope") != identity.scope:
        raise HTTPException(404, "Conversa não encontrada para o perfil atual.")
    return document


def new_user_conversation(identity):
    document = get_ai_api().create("conversations", {
        "user_id": identity.user_id, "status": "ACTIVE", "summarized_count": 0, "access_scope": identity.scope,
    })
    if document.get("access_scope") != identity.scope or document.get("user_id") != identity.user_id:
        raise HTTPException(503, "A API de persistência precisa suportar o escopo de acesso.")
    return document


def active_user_conversation(identity):
    result = get_ai_api().request("GET", "conversations", params={
        "userId": identity.user_id, "accessScope": identity.scope, "status": "ACTIVE", "page": 0, "size": 1,
    })
    if result["items"]:
        return authorized_conversation(identity, result["items"][0]["id"])
    return new_user_conversation(identity)


@app.get("/api/v1/me/chat", dependencies=[Depends(user_persistence)])
def my_access(identity: Principal = Depends(authenticated_user)):
    return {"userId": identity.user_id, "role": identity.role, "companyId": identity.company_id, "customerId": identity.customer_id,
            "sectorIds": identity.sector_ids,
            "domains": [name for name in ("faq", "estoque", "vendas", "clientes") if identity.allows(name)]}


@app.post("/api/v1/me/chat/conversations", dependencies=[Depends(user_persistence)], status_code=201)
def my_new_conversation(identity: Principal = Depends(authenticated_user)):
    document = new_user_conversation(identity)
    return {"id": str(document["_id"]), "status": document["status"]}


@app.get("/api/v1/me/chat/conversations", dependencies=[Depends(user_persistence)])
def my_conversations(page: int = Query(default=0, ge=0, le=10000), size: int = Query(default=20, ge=1, le=100),
                     identity: Principal = Depends(authenticated_user)):
    result = get_ai_api().request("GET", "conversations", params={
        "userId": identity.user_id, "accessScope": identity.scope, "page": page, "size": size,
    })
    result["items"] = [{key: item.get(key) for key in ("id", "status", "createdAt", "updatedAt")}
                       for item in result["items"]
                       if item.get("userId") == identity.user_id and item.get("accessScope") == identity.scope]
    return result


@app.get("/api/v1/me/chat/conversations/{conversation_id}", dependencies=[Depends(user_persistence)])
def my_conversation(conversation_id: str, identity: Principal = Depends(authenticated_user)):
    document = authorized_conversation(identity, conversation_id)
    # Não expõe detalhes internos de leases, execução ou autorização.
    return {"id": str(document["_id"]), "status": document["status"], "summary": document.get("summary"),
            "messages": [{"sender": item["sender"], "content": item["content"], "timestamp": item.get("timestamp"),
                          "sources": public_sources(item.get("sources")) if item["sender"] == "AI" else []}
                         for item in document.get("messages", [])]}


@app.post("/api/v1/me/chat/messages", response_model=ChatResponse, dependencies=[Depends(user_persistence)])
def my_message(request: PersonalChatRequest, identity: Principal = Depends(authenticated_user)):
    return process_message(ChatRequest(userId=identity.user_id, question=request.question,
                                       conversationId=request.conversation_id), identity)
