"""ASGI entry point for hosted (remote) deploys.

Serve with:
    uvicorn asgi:app --host 0.0.0.0 --port $PORT

This exposes the streamable-http app that claude.ai connects to. The local
(Claude Desktop / stdio) path does NOT use this file — it runs
`fmp_mcp_server.py` directly.
"""

import hmac
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fmp_mcp_server import mcp

app = mcp.streamable_http_app()


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require a private bearer token for every hosted MCP request."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path == "/health":
            return JSONResponse({"status": "ok"})

        expected = os.environ.get("FMP_MCP_ACCESS_TOKEN", "").strip()
        if not expected:
            return JSONResponse(
                {"error": "FMP_MCP_ACCESS_TOKEN is not configured"}, status_code=503
            )

        supplied = request.headers.get("authorization", "")
        prefix = "Bearer "
        candidate = supplied[len(prefix):] if supplied.startswith(prefix) else ""
        if not candidate or not hmac.compare_digest(candidate, expected):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        return await call_next(request)


app.add_middleware(BearerAuthMiddleware)
