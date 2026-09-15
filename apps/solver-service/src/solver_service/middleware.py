"""Pure-ASGI middleware: request-id + JSON access log, and body size limit."""

import logging
import time
import uuid

logger = logging.getLogger("solver_service.request")


class RequestContextMiddleware(object):
    """Assign a request id, emit ``x-request-id`` and one JSON access log line."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        start = time.monotonic()
        status = [None]

        async def send_with_context(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        finally:
            logger.info(
                "request",
                extra={
                    "ctx": {
                        "request_id": request_id,
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status[0],
                        "duration_ms": int((time.monotonic() - start) * 1000),
                    }
                },
            )


class MaxBodySizeMiddleware(object):
    """Reject request bodies larger than ``max_bytes`` with 413.

    Buffers the body (capped at max_bytes + 1, so at most the configured limit
    is ever held) and replays it to the app, which also covers chunked
    transfers without a Content-Length.
    """

    def __init__(self, app, max_bytes):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)

        for name, value in scope.get("headers") or []:
            if name == b"content-length" and int(value) > self.max_bytes:
                return await self._reject(send)

        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                return await self.app(scope, receive, send)
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self.max_bytes:
                return await self._reject(send)

        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, send):
        payload = b'{"detail":"request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": payload})
