"""Mobile Spring authentication uses only mocked HTTP and fictitious credentials."""
from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import auth
from app.config import settings
from app.access import Principal


def spring_transport(monkeypatch, handler):
    client_type = httpx.Client
    monkeypatch.setattr(auth.httpx, "Client", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(settings, "spring_auth_url", "https://spring.example.test")


def test_spring_mobile_uses_verified_backend_identity_without_cookies_or_firebase(monkeypatch):
    def verify(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v1/auth/me"
        assert request.headers["authorization"] == "Bearer opaque-test-token"
        assert "cookie" not in request.headers
        assert "x-csrf-token" not in request.headers
        return httpx.Response(200, json={"email": "own@example.test", "roles": ["EMPLOYEE"]})

    spring_transport(monkeypatch, verify)
    monkeypatch.setattr(settings, "firebase_project_id", None)
    firebase = Mock(side_effect=AssertionError("Firebase must not validate Spring tokens"))
    monkeypatch.setattr(auth, "firebase_identity", firebase)
    expected = Principal(7, "STOCKER", sector_ids=(2,), company_id=1)
    resolver = Mock(return_value=expected)
    monkeypatch.setattr(auth, "resolve_identity", resolver)
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer opaque-test-token"),
                       (b"cookie", b"unrelated=fictitious")]})
    assert auth.authenticated_user(request) == expected
    resolver.assert_called_once_with("own@example.test")
    firebase.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_rejected_backend_token_never_resolves_a_user(monkeypatch, status):
    spring_transport(monkeypatch, lambda _: httpx.Response(status, json={"detail": "private diagnostic"}))
    monkeypatch.setattr(settings, "firebase_project_id", None)
    resolver = Mock()
    monkeypatch.setattr(auth, "resolve_identity", resolver)
    with pytest.raises(HTTPException) as error:
        auth.authenticated_user(Request({"type": "http", "headers": [(b"authorization", b"Bearer test-token")]}))
    assert error.value.status_code == (status if status in {401, 403} else 503)
    assert "private diagnostic" not in error.value.detail
    resolver.assert_not_called()


@pytest.mark.parametrize("data", [{}, [], {"email": ""}, {"email": " "}, {"email": 7}])
def test_malformed_backend_identity_is_not_authorization(monkeypatch, data):
    spring_transport(monkeypatch, lambda _: httpx.Response(200, json=data))
    with pytest.raises(HTTPException) as error:
        auth.spring_bearer_identity("test-token")
    assert error.value.status_code == 503


@pytest.mark.parametrize("origin", [None, "http://remote.example", "https://user:password@example.test", "https://example.test/path"])
def test_invalid_authentication_origin_is_rejected_before_network(monkeypatch, origin):
    monkeypatch.setattr(settings, "spring_auth_url", origin)
    client = Mock(side_effect=AssertionError("No request allowed"))
    monkeypatch.setattr(auth.httpx, "Client", client)
    with pytest.raises(HTTPException) as error:
        auth.spring_bearer_identity("test-token")
    assert error.value.status_code == 503
    client.assert_not_called()


def test_spring_outage_does_not_fall_back_to_firebase(monkeypatch):
    spring_transport(monkeypatch, lambda _: httpx.Response(503))
    monkeypatch.setattr(settings, "firebase_project_id", "fictitious-project")
    firebase = Mock()
    monkeypatch.setattr(auth, "firebase_identity", firebase)
    with pytest.raises(HTTPException) as error:
        auth.authenticated_user(Request({"type": "http", "headers": [(b"authorization", b"Bearer test-token")]}))
    assert error.value.status_code == 503
    firebase.assert_not_called()


def test_legacy_firebase_is_still_explicitly_verified(monkeypatch):
    spring_transport(monkeypatch, lambda _: httpx.Response(401))
    monkeypatch.setattr(settings, "firebase_project_id", "fictitious-project")
    verifier = Mock(return_value="legacy@example.test")
    monkeypatch.setattr(auth, "firebase_identity", verifier)
    resolver = Mock(return_value=Principal(7, "CUSTOMER", customer_id=3, company_id=1))
    monkeypatch.setattr(auth, "resolve_identity", resolver)
    auth.authenticated_user(Request({"type": "http", "headers": [(b"authorization", b"Bearer legacy-test-token")]}))
    verifier.assert_called_once_with("legacy-test-token")
    resolver.assert_called_once_with("legacy@example.test")
