"""Reutiliza a sessão Spring no desktop e ID tokens Firebase no mobile."""
from functools import lru_cache
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request

from config import settings
from data.access import Principal
from data.postgres import executar_consulta


def spring_identity(request):
    base = settings.spring_auth_url
    if not base:
        raise HTTPException(503, "Autenticação desktop não configurada.")
    parsed = urlparse(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise HTTPException(503, "Origem da autenticação inválida.")
    cookie_names = {settings.spring_session_cookie_name, "KAIROS_ACCESS", "KAIROS_REFRESH", "KAIROS_CSRF"}
    cookies = {name: value for name, value in request.cookies.items() if name in cookie_names}
    csrf = request.headers.get("x-csrf-token")
    if not any(name in cookies for name in (settings.spring_session_cookie_name, "KAIROS_ACCESS", "KAIROS_REFRESH")):
        raise HTTPException(401, "Entre novamente no Kairos.")
    if not csrf:
        raise HTTPException(403, "Token CSRF obrigatório.")
    origin = request.headers.get("origin")
    if origin and origin not in settings.chat_allowed_origins:
        raise HTTPException(403, "Origem não autorizada.")
    try:
        # POST permite ao próprio Spring validar sessão e CSRF, sem replicar seu algoritmo.
        with httpx.Client(timeout=settings.ai_api_timeout_seconds, trust_env=False, follow_redirects=False,
                          cookies=cookies) as client:
            response = client.post(base.rstrip("/") + "/api/v1/auth/chat-context",
                                   headers={"X-CSRF-TOKEN": csrf})
        # O Spring pode renovar o acesso durante a validação. O navegador precisa receber esses cookies.
        request.state.auth_set_cookies = [value for value in response.headers.get_list("set-cookie")
                                          if value.split("=", 1)[0] in cookie_names]
        if response.status_code in {401, 403}:
            raise HTTPException(response.status_code, "Sessão ou token CSRF inválido.")
        if response.status_code != 200:
            raise HTTPException(503, "Autenticação desktop indisponível.")
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("email"), str) or not data["email"]:
            raise ValueError("Identidade ausente")
        return data["email"]
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(503, "Autenticação desktop indisponível.") from None


@lru_cache(maxsize=1)
def firebase_app():
    import firebase_admin
    return firebase_admin.initialize_app(options={"projectId": settings.firebase_project_id}, name="kairos-chat")


def firebase_identity(token):
    if not settings.firebase_project_id:
        raise HTTPException(503, "Autenticação mobile não configurada.")
    from firebase_admin import auth, exceptions
    try:
        claims = auth.verify_id_token(token, app=firebase_app(), check_revoked=True)
    except (auth.CertificateFetchError, exceptions.UnavailableError):
        raise HTTPException(503, "Autenticação mobile indisponível.") from None
    except (auth.InvalidIdTokenError, auth.RevokedIdTokenError, auth.UserDisabledError, auth.UserNotFoundError, ValueError):
        raise HTTPException(401, "Token mobile inválido ou expirado.") from None
    except Exception:
        raise HTTPException(503, "Autenticação mobile indisponível.") from None
    if claims.get("email_verified") is not True or not isinstance(claims.get("email"), str) or not claims["email"]:
        raise HTTPException(403, "Verifique seu e-mail antes de acessar o chatbot.")
    return claims["email"]


def resolve_identity(email):
    # Perfis e vínculos vêm do PostgreSQL, nunca de roles enviadas pelo cliente/Firebase/Firestore.
    rows = executar_consulta("""
        SELECT u.id, c.id, e.id, e.position, e.company_id, c.company_id
        FROM users u
        LEFT JOIN customers c ON c.users_id = u.id
        LEFT JOIN employees e ON e.users_id = u.id
        WHERE u.email = %(email)s
    """, {"email": email})
    if len(rows) != 1:
        raise HTTPException(403, "Conta sem vínculo com o Kairos.")
    user_id, customer_id, employee_id, position, employee_company, customer_company = rows[0]
    if not isinstance(user_id, int) or not 0 < user_id <= 2147483647:
        raise HTTPException(403, "Identificador de conta incompatível.")
    if position == "MANAGER":
        if employee_company is None:
            raise HTTPException(403, "Funcionário sem empresa vinculada.")
        return Principal(user_id, "MANAGER", company_id=employee_company)
    if position == "STOCKER":
        if employee_company is None:
            raise HTTPException(403, "Funcionário sem empresa vinculada.")
        sectors = executar_consulta("""
            SELECT DISTINCT a.sector_id FROM employee_aisles ea
            JOIN aisles a ON a.id = ea.aisle_id JOIN sectors sec ON sec.id = a.sector_id
            WHERE ea.employee_id = %(employee_id)s AND sec.company_id = %(company_id)s
            UNION
            SELECT DISTINCT a.sector_id FROM shelf_employees se
            JOIN shelves s ON s.id = se.shelf_id JOIN aisles a ON a.id = s.aisle_id
            JOIN sectors sec ON sec.id = a.sector_id
            WHERE se.employee_id = %(employee_id)s AND sec.company_id = %(company_id)s
        """, {"employee_id": employee_id, "company_id": employee_company})
        return Principal(user_id, "STOCKER", sector_ids=tuple(sorted({row[0] for row in sectors})), company_id=employee_company)
    if customer_id is not None:
        if customer_company is None:
            raise HTTPException(403, "Cliente sem empresa vinculada.")
        return Principal(user_id, "CUSTOMER", customer_id=customer_id, company_id=customer_company)
    raise HTTPException(403, "Perfil sem acesso ao chatbot.")


def authenticated_user(request: Request):
    authorization = request.headers.get("authorization")
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 16384:
            raise HTTPException(401, "Credencial inválida.")
        email = firebase_identity(token)
    else:
        email = spring_identity(request)
    try:
        return resolve_identity(email)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Não foi possível consultar as permissões.") from None
