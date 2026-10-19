"""Cliente interno da API Java; não transmite credenciais do banco ao chatbot."""
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urlparse

import httpx
from bson import ObjectId

from config import settings
from data.request_context import chat_context


class PersistenceError(RuntimeError):
    def __init__(self, status=503):
        super().__init__("A API de persistência não pôde concluir a operação.")
        self.status = status


def via_api():
    return settings.persistence_backend == "api"


def _camel(key):
    first, *parts = key.split("_")
    return first + "".join(part.capitalize() for part in parts)


def _snake(key):
    return "".join("_" + c.lower() if c.isupper() else c for c in key)


def _value(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
    if isinstance(value, dict):
        return {k: _value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_value(v) for v in value]
    return value


def encode(document):
    result = {}
    for key, value in document.items():
        if key in {"messages", "memories"} and value is not None:
            value = [encode(item) for item in value]
        elif key in {"created_at", "updated_at", "started_at", "completed_at", "timestamp", "summary_updated_at"} and isinstance(value, str):
            # Formato legado do console; datas da API sempre carregam o fuso.
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            value = instant.replace(tzinfo=instant.tzinfo or timezone.utc).isoformat()
        result[_camel(key)] = _value(value)
    return result


def decode(document):
    result = {}
    for key, value in document.items():
        key = "_id" if key == "id" else _snake(key)
        if key in {"messages", "memories"} and value is not None:
            value = [decode(item) for item in value]
        elif key in {"_id", "execution_id", "conversation_id", "document_id", "memory_id"} and value is not None:
            value = ObjectId(value)
        result[key] = value
    return result


class AiApiClient:
    def __init__(self, base_url, token, *, transport=None):
        parsed = urlparse(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("AI_API_URL deve ser uma origem HTTPS ou HTTP local.")
        if not token or len(token) < 32:
            raise ValueError("AI_API_TOKEN deve conter ao menos 32 caracteres.")
        self.http = httpx.Client(base_url=base_url.rstrip("/") + "/api/v1/",
                                 headers={"Authorization": "Bearer " + token},
                                 timeout=settings.ai_api_timeout_seconds, transport=transport,
                                 trust_env=False, follow_redirects=False)

    def request(self, method, path, *, payload=None, params=None):
        try:
            context = chat_context.get()
            headers = {"X-Chat-Lease": context.request_id} if context is not None else None
            response = self.http.request(method, path, json=payload, params=params, headers=headers)
        except httpx.HTTPError:
            raise PersistenceError() from None
        if not response.is_success:
            raise PersistenceError(response.status_code)
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise PersistenceError() from None

    def get(self, resource, id):
        return decode(self.request("GET", f"{resource}/{id}"))

    def create(self, resource, document):
        return decode(self.request("POST", resource, payload=encode(document)))

    def patch(self, resource, id, fields):
        return decode(self.request("PATCH", f"{resource}/{id}", payload=encode(fields)))

    def list(self, resource, **filters):
        for page in range(10001):
            data = self.request("GET", resource, params={**filters, "page": page, "size": 100})
            for item in data["items"]:
                yield decode(item)
            if not data["hasNext"]:
                return
        raise PersistenceError()

    def active_conversation(self, user_id):
        return decode(self.request("POST", f"runtime/users/{user_id}/conversations/active"))

    def conversation(self, user_id, id):
        return decode(self.request("GET", f"runtime/users/{user_id}/conversations/{id}"))

    def append_turn(self, user_id, id, question, answer, turn_id):
        return self.request("POST", f"runtime/users/{user_id}/conversations/{id}/turns",
                            payload={"turnId": turn_id, "question": question, "answer": answer})

    def summarize(self, user_id, id, summary, old, new, previous):
        try:
            self.request("PATCH", f"runtime/users/{user_id}/conversations/{id}/summary", payload={
                "summary": summary, "previousSummary": previous,
                "previousCount": old, "summarizedCount": new})
        except PersistenceError as error:
            if error.status == 409:
                return False
            raise
        return True

    def append_memories(self, user_id, memories):
        return self.request("POST", f"runtime/users/{user_id}/memories", payload={"memories": [encode(m) for m in memories]})

    def lease(self, user_id, id, owner, *, action="acquire"):
        suffix = "" if action == "acquire" else "/" + action
        return self.request("POST", f"runtime/users/{user_id}/conversations/{id}/lease{suffix}", payload={"owner": owner})


@lru_cache(maxsize=1)
def get_ai_api():
    if not settings.ai_api_url or not settings.ai_api_token:
        raise RuntimeError("Configure AI_API_URL e AI_API_TOKEN antes de habilitar a integração.")
    return AiApiClient(settings.ai_api_url, settings.ai_api_token.get_secret_value())
