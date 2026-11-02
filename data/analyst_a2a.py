"""A2A 1.0 via SDK oficial; identidade assinada pelo backend, nunca pelo LLM."""
import asyncio
from urllib.parse import urlparse

import httpx
import jwt
from anyio import from_thread
from a2a import helpers
from a2a.client import ClientConfig, create_client
from a2a.server.agent_execution import AgentExecutor
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import ServerCallContextBuilder, StarletteUser
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Role, SendMessageRequest
from google.protobuf.json_format import MessageToDict
from langchain_core.messages import AIMessage, ToolMessage
from langsmith.run_helpers import get_current_run_tree, tracing_context
from starlette.authentication import SimpleUser
from starlette.responses import JSONResponse

from config import settings
from data.access import principal
from data.auth import analyst_signing_key, create_analyst_token, verify_analyst_token
from data.answer_sources import consulted, public_sources

RPC_PATH = "/a2a/analyst"
CARD_PATH = "/a2a/analyst/.well-known/agent-card.json"


def endpoint():
    value = settings.analyst_a2a_url or "http://127.0.0.1" + RPC_PATH
    parsed = urlparse(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path != RPC_PATH
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise RuntimeError("Configure ANALYST_A2A_URL com o endpoint HTTPS do analista ou loopback local.")
    return value


def agent_card(url):
    return AgentCard(name="Kairos Analista", description="Insights históricos e operacionais para gerentes.",
                     version="1.0.0", default_input_modes=["text/plain"], default_output_modes=["application/json"],
                     capabilities=AgentCapabilities(streaming=False),
                     supported_interfaces=[AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0")],
                     security_schemes={"backend": {"http_auth_security_scheme": {"scheme": "bearer", "bearer_format": "JWT"}}},
                     security_requirements=[{"schemes": {"backend": {}}}],
                     skills=[AgentSkill(id="insights", name="Análise gerencial", tags=["analytics", "mcp"],
                                        description="Combina indicadores autorizados com especialistas de estoque e vendas.")])


class AnalystContext(ServerCallContextBuilder):
    def build(self, request):
        identity = request.state.analyst_identity
        return ServerCallContext(user=StarletteUser(SimpleUser(identity.scope)), state={
            "principal": identity, "headers": {"a2a-version": request.headers.get("a2a-version", "")},
            "trace_parent": request.headers.get("langsmith-trace"),
            "mcp": getattr(request.app.state, "analyst_mcp", None),
        })


class AnalystExecutor(AgentExecutor):
    async def execute(self, context, event_queue):
        from data.analyst_runtime import run_analyst
        question = context.get_user_input()
        if not question.strip() or len(question) > 15000:
            payload = {"answer": "Informe uma pergunta de até 15.000 caracteres.", "evidence": [], "sources": []}
        else:
            if context.call_context.state["mcp"] is None:
                raise RuntimeError("O ciclo de vida da aplicação precisa inicializar o MCP.")
            parent = context.call_context.state.get("trace_parent")
            with tracing_context(parent={"langsmith-trace": parent} if parent else None):
                payload = await asyncio.wait_for(run_analyst(question, context.call_context.state["principal"],
                                                            context.call_context.state["mcp"]),
                                                 timeout=settings.analyst_timeout_seconds - 5)
        message = helpers.new_text_message("Análise concluída.")
        del message.parts[:]
        message.parts.append(helpers.new_data_part(payload))
        await event_queue.enqueue_event(message)

    async def cancel(self, context, event_queue):
        raise NotImplementedError("Este analista responde em uma única mensagem.")


def install_analyst_routes(app):
    @app.middleware("http")
    async def analyst_auth(request, call_next):
        if request.url.path.rstrip("/") == RPC_PATH:
            try:
                analyst_signing_key()
            except RuntimeError:
                return JSONResponse({"detail": "Analista não configurado."}, status_code=503)
            try:
                header = request.headers.get("authorization", "")
                if not header.startswith("Bearer "):
                    raise jwt.InvalidTokenError()
                request.state.analyst_identity = verify_analyst_token(header[7:])
            except PermissionError:
                return JSONResponse({"detail": "Perfil sem acesso ao analista."}, status_code=403)
            except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
                return JSONResponse({"detail": "Credencial A2A inválida."}, status_code=401)
        return await call_next(request)

    # Sem URL, as outras rotas da API permanecem disponíveis; o analista falha fechado.
    try:
        url = endpoint()
    except RuntimeError:
        url = "http://127.0.0.1:8000" + RPC_PATH
    card = agent_card(url)
    handler = DefaultRequestHandler(AnalystExecutor(), InMemoryTaskStore(), agent_card=card)
    app.router.routes.extend(create_agent_card_routes(card, card_url=CARD_PATH))
    app.router.routes.extend(create_jsonrpc_routes(handler, rpc_url=RPC_PATH, context_builder=AnalystContext()))


async def request_analysis(question, identity, http=None):
    if http is None and not settings.analyst_a2a_url:
        from api import app
        # API em execução reutiliza seu ciclo de vida; console inicializa/encerra por código.
        if getattr(app.state, "analyst_mcp", None) is None:
            async with app.router.lifespan_context(app):
                return await request_analysis(question, identity)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                      timeout=settings.analyst_timeout_seconds, trust_env=False) as local:
            return await request_analysis(question, identity, local)
    url = endpoint()
    token = create_analyst_token(identity)
    own_http = http is None
    http = http or httpx.AsyncClient(timeout=settings.analyst_timeout_seconds, trust_env=False, follow_redirects=False)
    try:
        # Endpoint do transporte é configuração do servidor; não segue URLs escolhidas por um modelo.
        http.headers.update({"Authorization": "Bearer " + token, "A2A-Version": "1.0"})
        parent = get_current_run_tree()
        if parent is not None:
            http.headers.update({"langsmith-trace": parent.to_headers()["langsmith-trace"]})
        client = await create_client(agent_card(url), ClientConfig(httpx_client=http, streaming=False,
                                                                 supported_protocol_bindings=["JSONRPC"]))
        request = SendMessageRequest(message=helpers.new_text_message(question, role=Role.ROLE_USER))
        async for event in client.send_message(request):
            if event.HasField("message"):
                for part in event.message.parts:
                    if part.HasField("data"):
                        result = MessageToDict(part.data)
                        if isinstance(result, dict) and isinstance(result.get("answer"), str):
                            return result
        raise RuntimeError("O analista não retornou uma resposta válida.")
    finally:
        if own_http:
            await http.aclose()


def invoke_analyst(payload, **kwargs):
    identity = principal()
    if identity is None or identity.role != "MANAGER" or identity.company_id is None:
        raise PermissionError("Análise estratégica exige identidade de gerente e empresa verificadas.")
    question = "\n".join(message.text for message in payload["messages"])[-15000:]
    from api import app
    loop_token = getattr(app.state, "analyst_loop_token", None)
    if loop_token is not None:
        # O grafo síncrono roda em worker; o MCP permanece no loop que o iniciou.
        result = from_thread.run(request_analysis, question, identity, token=loop_token)
    else:
        result = asyncio.run(request_analysis(question, identity))
    messages = list(payload["messages"])
    for index, evidence in enumerate(result.get("evidence", [])):
        messages.append(ToolMessage(content=evidence["conteudo"], name=evidence["ferramenta"],
                                    tool_call_id=f"analyst-{index}", status=evidence["status"]))
    for source in public_sources(result.get("sources")):
        consulted(source)
    return {"messages": [*messages, AIMessage(content=result["answer"])]}
