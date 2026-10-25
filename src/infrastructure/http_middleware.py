"""
Pure-ASGI middleware for HTTP transport:
- BodySizeLimitMiddleware: Buffers request body with size and timeout guards.
"""
import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger("inatrace-mcp.http")

MCP_LINK_HEADER = '</mcp/sse>; rel="modelcontextprotocol"'


class BodySizeLimitMiddleware:
    """
    Pure-ASGI guard that buffers the request body up to a cap before the application
    ever sees it, and replays it from memory.

    Rejecting after the inner app has begun responding is not possible, so the body is
    read here: a request over the cap is answered 413 without reaching a handler, and
    memory stays bounded by the cap even for chunked bodies that declare no
    Content-Length. The read is also bounded in time, so a client that opens a request
    and stalls cannot hold a worker indefinitely.
    """

    def __init__(self, app, max_bytes: int, read_timeout: float):
        self.app = app
        self.max_bytes = max_bytes
        self.read_timeout = read_timeout

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)

        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                declared_len = int(declared)
            except ValueError:
                return await self._reject(send, 400, "BAD_CONTENT_LENGTH",
                                          "La cabecera Content-Length no es un número.")
            if declared_len > self.max_bytes:
                return await self._too_large(send, declared_len)

        body = bytearray()
        more_body = True
        while more_body:
            try:
                message = await asyncio.wait_for(receive(), timeout=self.read_timeout)
            except asyncio.TimeoutError:
                logger.warning("Lectura del cuerpo abortada por timeout.")
                return await self._reject(send, 408, "REQUEST_TIMEOUT",
                                          "El cuerpo de la petición no se recibió a tiempo.")
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                return await self._too_large(send, len(body))
            more_body = message.get("more_body", False)

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    async def _too_large(self, send, size: int):
        logger.warning(
            f"Petición rechazada: {size} bytes exceden el límite de {self.max_bytes}."
        )
        return await self._reject(
            send, 413, "REQUEST_TOO_LARGE",
            f"El cuerpo de la petición excede el límite de {self.max_bytes} bytes."
        )

    async def _reject(self, send, status: int, code: str, message: str):
        payload = json.dumps({"error": code, "message": message}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(payload)).encode()),
                (b"link", MCP_LINK_HEADER.encode()),
            ],
        })
        await send({"type": "http.response.body", "body": payload})
