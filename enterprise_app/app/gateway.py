import time
import uuid
from collections import defaultdict, deque
from threading import Lock

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings


class ApiGatewayMiddleware(BaseHTTPMiddleware):
    """Lightweight gateway controls: client identity, request IDs, and rate limiting."""

    def __init__(self, app):
        super().__init__(app)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id

        path = request.url.path
        method = request.method.upper()
        needs_gateway_controls = path.startswith(settings.api_prefix)

        if needs_gateway_controls and method != "OPTIONS":
            client_id = request.headers.get(settings.gateway_client_header, "").strip()
            if not client_id:
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": f"Missing required header: {settings.gateway_client_header}",
                        "request_id": request_id,
                    },
                )
            if self._is_rate_limited(client_id):
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "Rate limit exceeded. Try again in a minute.",
                        "request_id": request_id,
                        "client_id": client_id,
                    },
                )
            request.state.client_id = client_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    def _is_rate_limited(self, client_id: str) -> bool:
        now = time.time()
        minute_ago = now - 60
        with self._lock:
            bucket = self._hits[client_id]
            while bucket and bucket[0] < minute_ago:
                bucket.popleft()
            if len(bucket) >= settings.gateway_rate_limit_per_minute:
                return True
            bucket.append(now)
            return False

