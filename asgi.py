"""ASGI entry point for hosted (remote) deploys.

Serve with:
    uvicorn asgi:app --host 0.0.0.0 --port $PORT

This exposes the streamable-http app that claude.ai connects to. The local
(Claude Desktop / stdio) path does NOT use this file — it runs
`fmp_mcp_server.py` directly.
"""

from fmp_mcp_server import mcp

app = mcp.streamable_http_app()
