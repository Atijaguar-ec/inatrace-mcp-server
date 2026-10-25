"""
HTTP/SSE transport for the INATrace MCP Server, on Starlette + Uvicorn.

Replaces the hand-rolled `asyncio.start_server` handler and its manual HTTP parser,
which had no request size limit (an arbitrary Content-Length could exhaust memory),
no read timeouts and no connection limits (QA finding ALT-01).

Routes are unchanged so existing clients keep working:

    GET  /health/live, /health        liveness
    GET  /health/ready               readiness (503 without the read-only pool)
    GET  /mcp/tools,     /tools       tool inventory
    GET  /mcp/resources, /resources   resource inventory
    GET  /mcp/prompts,   /prompts     prompt inventory
    GET  /mcp/sse                     MCP endpoint announcement (Server-Sent Events)
    POST /mcp, /jsonrpc               JSON-RPC 2.0 dispatcher
    POST /mcp/tools/{tool}, /tools/{tool}   single tool invocation

Every tool invocation goes through `execute_authorized_tool`, so authorization, the
tier argument policy and the tenant boundary apply on both transports.
"""
import json
import time
import uuid
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse, Response
from starlette.routing import Route

from src.config import settings
from src.infrastructure.db_pool import get_db_pool, DatabaseUnavailableError
from src.infrastructure.spring_client import ServiceTokenUnavailableError
from src.infrastructure.http_middleware import BodySizeLimitMiddleware
from src.infrastructure.mcp_session import MCPSession, MCPSessionManager
from src.infrastructure.keycloak_guard import expected_issuer

logger = logging.getLogger("inatrace-mcp.http")

MCP_LINK_HEADER = '</mcp/sse>; rel="modelcontextprotocol"'


def _json(payload: Dict[str, Any], status_code: int = 200, headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    """JSON response carrying the MCP discovery Link header."""
    merged = {"Link": MCP_LINK_HEADER}
    if headers:
        merged.update(headers)
    return JSONResponse(payload, status_code=status_code, headers=merged)


session_manager = MCPSessionManager()


def build_app(mcp) -> Starlette:
    """Builds the ASGI application for an INATraceFastMCP instance."""
    from src.app import execute_authorized_tool

    async def health_live(request: Request) -> Response:
        return _json({
            "status": "ALIVE",
            "server": mcp.name,
            "tools_count": len(mcp.tools),
            "resources_count": len(mcp.resources),
            "prompts_count": len(mcp.prompts),
            "whisp_source": "plotdeforestationanalysis"
        })

    async def health_ready(request: Request) -> Response:
        pool = get_db_pool()
        return _json({
            "status": "READY" if pool else "DEGRADED",
            "db_pool_connected": pool is not None,
            "whisp_table": "plotdeforestationanalysis"
        }, status_code=200 if pool else 503)

    async def list_tools(request: Request) -> Response:
        return _json({
            "server": mcp.name,
            "tools": [
                {"name": k, "description": (fn.__doc__ or "").strip()}
                for k, fn in mcp.tools.items()
            ]
        })

    async def list_resources(request: Request) -> Response:
        return _json({
            "server": mcp.name,
            "resources": [
                {
                    "uri": r["uri"],
                    "name": r["name"],
                    "description": r["description"],
                    "mimeType": r["mimeType"]
                }
                for r in mcp.resources.values()
            ]
        })

    async def list_prompts(request: Request) -> Response:
        return _json({
            "server": mcp.name,
            "prompts": [
                {"name": p["name"], "description": p["description"]}
                for p in mcp.prompts.values()
            ]
        })

    async def sse_endpoint(request: Request) -> Response:
        """
        Announces the MCP endpoint to an AI-capable client and provides SSE transport.
        Returns the endpoint discovery payload with sessionId, and supports persistent
        event streaming when stream=true.
        """
        lot_id = request.query_params.get("lotId", "unknown")
        session_id = request.headers.get("mcp-session-id") or uuid.uuid4().hex
        prefix = "/mcp" if request.url.path.startswith("/mcp") else ""

        payload = json.dumps({
            "protocol": "mcp/2024-11-05",
            "server": mcp.name,
            "lotId": lot_id,
            "sessionId": session_id,
            "jsonrpcEndpoint": "/mcp",
            "messageEndpoint": f"{prefix}/message?sessionId={session_id}",
            "availableTools": list(mcp.tools.keys()),
            "availableResources": list(mcp.resources.keys()),
            "availablePrompts": list(mcp.prompts.keys())
        })

        is_stream = request.query_params.get("stream") == "true"

        async def event_stream():
            yield f"event: endpoint\ndata: {payload}\n\n".encode("utf-8")
            if is_stream:
                session = await session_manager.create_session(auth_header=request.headers.get("authorization"))
                try:
                    while True:
                        try:
                            msg = await asyncio.wait_for(session.queue.get(), timeout=15.0)
                            yield f"event: message\ndata: {msg}\n\n".encode("utf-8")
                        except asyncio.TimeoutError:
                            yield f": keepalive\n\n".encode("utf-8")
                except (asyncio.CancelledError, GeneratorExit):
                    pass
                finally:
                    await session_manager.remove_session(session.session_id)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Mcp-Session-Id": session_id,
                "Link": f'<{prefix}/sse?lotId={lot_id}>; rel="modelcontextprotocol"'
            }
        )

    async def post_message(request: Request) -> Response:
        """
        Handles incoming JSON-RPC 2.0 messages for an active MCP SSE session.
        Enqueues the response back to the client's SSE event stream.
        """
        session_id = request.query_params.get("sessionId") or request.headers.get("mcp-session-id")
        if not session_id:
            return _json(
                {"error": "MISSING_SESSION_ID", "message": "Parámetro 'sessionId' o cabecera 'Mcp-Session-Id' requerida."},
                status_code=400
            )

        session = await session_manager.get_session(session_id)
        if not session:
            return _json(
                {"error": "SESSION_NOT_FOUND", "message": f"Sesión '{session_id}' no encontrada o expirada."},
                status_code=404
            )

        try:
            body = await request.body()
            rpc_req = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            return _json({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"}
            }, status_code=400)

        if not isinstance(rpc_req, dict):
            return _json({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: se esperaba un objeto JSON"}
            }, status_code=400)

        auth_header = request.headers.get("authorization") or session.auth_header
        rpc_resp = await mcp.handle_jsonrpc_request(rpc_req, auth_header=auth_header)

        try:
            session.queue.put_nowait(json.dumps(rpc_resp))
        except asyncio.QueueFull:
            return _json({"error": "QUEUE_FULL", "message": "Cola de mensajes llena para esta sesión."}, status_code=503)

        return Response(content="Accepted", status_code=202, media_type="text/plain", headers={"Mcp-Session-Id": session_id})

    async def jsonrpc(request: Request) -> Response:
        session_id = request.headers.get("mcp-session-id") or request.query_params.get("sessionId")
        try:
            rpc_req = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError as exc:
            return _json({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"}
            })
        if not isinstance(rpc_req, dict):
            return _json({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: se esperaba un objeto JSON"}
            })

        if not session_id and rpc_req.get("method") == "initialize":
            session_id = uuid.uuid4().hex

        auth_header = request.headers.get("authorization")
        resp = await mcp.handle_jsonrpc_request(rpc_req, auth_header=auth_header)
        headers = {"Mcp-Session-Id": session_id} if session_id else {}
        return _json(resp, headers=headers)

    async def oauth_protected_resource(request: Request) -> Response:
        """
        RFC 8414 / RFC 9207 OAuth 2.0 Protected Resource Metadata.
        """
        realm_issuer = expected_issuer()
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        resource_url = f"{proto}://{host}".rstrip("/")
        return _json({
            "resource": resource_url,
            "authorization_servers": [realm_issuer],
            "scopes_supported": ["openid", "profile", "email"],
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://github.com/atijaguar-ec/inatrace-mcp-server"
        })

    async def call_tool(request: Request) -> Response:
        tool_name = request.path_params["tool_name"]
        if tool_name not in mcp.tools:
            return _json({"error": "NOT_FOUND", "message": f"Tool '{tool_name}' not found"},
                         status_code=404)

        try:
            raw = await request.body()
            params = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            return _json({"error": "BAD_REQUEST", "message": f"JSON inválido: {exc}"},
                         status_code=400)
        if not isinstance(params, dict):
            return _json({"error": "BAD_REQUEST", "message": "Se esperaba un objeto JSON"},
                         status_code=400)

        try:
            result = await asyncio.wait_for(
                execute_authorized_tool(
                    mcp.tools[tool_name], tool_name, params,
                    request.headers.get("authorization")
                ),
                timeout=settings.TOOL_TIMEOUT_SECONDS
            )
            return _json(result)
        except PermissionError as pe:
            return _json({"error": "FORBIDDEN", "message": str(pe)}, status_code=403)
        except DatabaseUnavailableError as due:
            return _json({"error": "DATABASE_UNAVAILABLE", "message": str(due)}, status_code=503)
        except ServiceTokenUnavailableError as ste:
            return _json({"error": "SERVICE_TOKEN_UNAVAILABLE", "message": str(ste)}, status_code=503)
        except asyncio.TimeoutError:
            logger.error(f"'{tool_name}' excedió {settings.TOOL_TIMEOUT_SECONDS}s y se abortó.")
            return _json({
                "error": "TOOL_TIMEOUT",
                "message": f"'{tool_name}' excedió el límite de {settings.TOOL_TIMEOUT_SECONDS}s."
            }, status_code=504)
        except Exception as exc:
            logger.error(f"Error ejecutando '{tool_name}': {exc}")
            return _json({"error": "EXECUTION_ERROR", "message": str(exc)}, status_code=400)

    async def not_found(request: Request, exc: Exception) -> Response:
        return _json({"error": "NOT_FOUND", "path": request.url.path}, status_code=404)

    routes = [
        Route("/health", health_live, methods=["GET"]),
        Route("/health/live", health_live, methods=["GET"]),
        Route("/health/ready", health_ready, methods=["GET"]),
        Route("/mcp/tools", list_tools, methods=["GET"]),
        Route("/tools", list_tools, methods=["GET"]),
        Route("/mcp/resources", list_resources, methods=["GET"]),
        Route("/resources", list_resources, methods=["GET"]),
        Route("/mcp/prompts", list_prompts, methods=["GET"]),
        Route("/prompts", list_prompts, methods=["GET"]),
        Route("/mcp/sse", sse_endpoint, methods=["GET"]),
        Route("/sse", sse_endpoint, methods=["GET"]),
        Route("/mcp/message", post_message, methods=["POST"]),
        Route("/message", post_message, methods=["POST"]),
        Route("/mcp", jsonrpc, methods=["POST"]),
        Route("/jsonrpc", jsonrpc, methods=["POST"]),
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource, methods=["GET"]),
        Route("/mcp/.well-known/oauth-protected-resource", oauth_protected_resource, methods=["GET"]),
        Route("/mcp/tools/{tool_name}", call_tool, methods=["POST"]),
        Route("/tools/{tool_name}", call_tool, methods=["POST"]),
    ]

    @asynccontextmanager
    async def lifespan(app):
        await mcp.run_startup()
        try:
            yield
        finally:
            await mcp.run_shutdown()

    return Starlette(
        routes=routes,
        exception_handlers={404: not_found},
        lifespan=lifespan,
        middleware=[
            Middleware(
                BodySizeLimitMiddleware,
                max_bytes=settings.MAX_REQUEST_BYTES,
                read_timeout=settings.BODY_READ_TIMEOUT_SECONDS
            )
        ],
    )
