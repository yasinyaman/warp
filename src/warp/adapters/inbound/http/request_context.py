"""Per-request identifiers shared by logging and the audit trail."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

REQUEST_ID_HEADER = "X-Request-ID"


def request_id_of(request: Request) -> str:
    """The id assigned to this request (empty when the middleware is absent)."""
    return str(getattr(request.state, "request_id", "") or "")


def add_request_id_middleware(app: FastAPI) -> None:
    """Give every request an id, and echo it back.

    A caller-supplied ``X-Request-ID`` is honoured so one identifier can follow
    a request across services; otherwise one is generated. It is the only thing
    tying an audit event to the application logs for the same request.
    """

    @app.middleware("http")
    async def assign_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
        # Bounded and sanitized: it is echoed in a header and written to the
        # audit log, so it must not carry arbitrary caller-controlled text.
        request_id = (
            incoming[:64]
            if incoming and incoming[:64].replace("-", "").replace("_", "").isalnum()
            else uuid.uuid4().hex
        )
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
