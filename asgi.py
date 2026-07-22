"""ASGI entry point for the hosted BCNA FMP MCP server.

Supports two bearer-token methods:
1. Auth0 OAuth access tokens for ChatGPT.
2. A private static token for the local Codex plugin.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fmp_mcp_server import mcp


app = mcp.streamable_http_app()


def _setting(name: str, *, required: bool = False) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise RuntimeError(f"{name} is required for hosted OAuth")
    return value


def _public_base_url(request: Request) -> str:
    configured = _setting("MCP_PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def _required_scopes() -> set[str]:
    raw = _setting("OAUTH_REQUIRED_SCOPES") or "fmp:read"
    return {scope for scope in raw.replace(",", " ").split() if scope}


def _oauth_metadata(request: Request) -> dict[str, Any]:
    base_url = _public_base_url(request)
    issuer = _setting("OAUTH_ISSUER", required=True)
    return {
        "resource": _setting("OAUTH_AUDIENCE") or base_url,
        "authorization_servers": [issuer],
        "scopes_supported": sorted(_required_scopes()),
        "resource_documentation": f"{base_url}/docs/authentication",
        "bearer_methods_supported": ["header"],
    }


def _challenge(request: Request) -> str:
    base_url = _public_base_url(request)
    scopes = " ".join(sorted(_required_scopes()))
    return (
        'Bearer resource_metadata="'
        f"{base_url}/.well-known/oauth-protected-resource"
        f'", scope="{scopes}"'
    )


def _unauthorized(request: Request, message: str = "Unauthorized") -> JSONResponse:
    return JSONResponse(
        {"error": message},
        status_code=401,
        headers={"WWW-Authenticate": _challenge(request)},
    )


def _scope_set(payload: dict[str, Any]) -> set[str]:
    scope = payload.get("scope", "")
    if isinstance(scope, str):
        return set(scope.split())
    permissions = payload.get("permissions", [])
    if isinstance(permissions, list):
        return {str(item) for item in permissions}
    return set()


def _validate_oauth_token(token: str) -> dict[str, Any]:
    issuer = _setting("OAUTH_ISSUER", required=True)
    audience = _setting("OAUTH_AUDIENCE", required=True)
    algorithms = [
        item.strip()
        for item in (_setting("OAUTH_ALGORITHMS") or "RS256").split(",")
        if item.strip()
    ]
    jwks_url = _setting("OAUTH_JWKS_URL") or (
        f"{issuer.rstrip('/')}/.well-known/jwks.json"
    )
    signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(token).key
    payload = jwt.decode(
        token,
        signing_key,
        algorithms=algorithms,
        audience=audience,
        issuer=issuer,
        options={"require": ["exp", "iat", "iss", "aud"]},
    )
    missing = _required_scopes() - _scope_set(payload)
    if missing:
        raise jwt.InvalidTokenError(
            f"Token is missing required scope(s): {', '.join(sorted(missing))}"
        )
    return payload


class HostedAuthMiddleware(BaseHTTPMiddleware):
    """Expose discovery routes and protect MCP with OAuth or a static token."""

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path.rstrip("/") or "/"

        if path == "/health":
            return JSONResponse({"status": "ok"})

        if path in {
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        }:
            try:
                return JSONResponse(_oauth_metadata(request))
            except RuntimeError as exc:
                return JSONResponse({"error": str(exc)}, status_code=503)

        if path == "/docs/authentication":
            return JSONResponse(
                {
                    "authentication": "OAuth 2.1 bearer token",
                    "scope": " ".join(sorted(_required_scopes())),
                    "local_codex": (
                        "A private FMP_MCP_ACCESS_TOKEN is also accepted "
                        "when mixed authentication is enabled."
                    ),
                }
            )

        supplied = request.headers.get("authorization", "")
        prefix = "Bearer "
        token = supplied[len(prefix) :].strip() if supplied.startswith(prefix) else ""
        if not token:
            return _unauthorized(request)

        auth_mode = (_setting("FMP_MCP_AUTH_MODE") or "mixed").lower()
        static_token = _setting("FMP_MCP_ACCESS_TOKEN")
        if auth_mode in {"mixed", "static"} and static_token:
            if hmac.compare_digest(token, static_token):
                return await call_next(request)

        if auth_mode in {"mixed", "oauth"}:
            try:
                _validate_oauth_token(token)
                return await call_next(request)
            except (PyJWTError, RuntimeError, ValueError):
                return _unauthorized(request, "Invalid or insufficient bearer token")

        return _unauthorized(request)


app.add_middleware(HostedAuthMiddleware)
