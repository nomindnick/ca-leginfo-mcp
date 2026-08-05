"""Per-client rate limiting for the HTTP transport (SPEC §3: read-only
public legal data; simple rate limiting).

Pure-ASGI sliding-window limiter, in-process state — the deployment is a
single Railway instance, so no shared store is needed. The client key is
the leftmost X-Forwarded-For entry (Railway's edge sets it) falling back
to the socket peer. /health is exempt so platform checks never trip it.
"""

from __future__ import annotations

import json
import time
from collections import deque

EXEMPT_PATHS = frozenset({"/health"})


class RateLimitMiddleware:
    def __init__(self, app, per_minute: int = 120):
        self.app = app
        self.per_minute = per_minute
        self.window = 60.0
        self.hits: dict[str, deque[float]] = {}

    def _client(self, scope) -> str:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                return value.decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self.hits.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        if len(self.hits) > 10_000:  # bound memory across many clients
            for k in [k for k, v in self.hits.items() if not v]:
                del self.hits[k]
        return True

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in EXEMPT_PATHS \
                or self.allow(self._client(scope)):
            await self.app(scope, receive, send)
            return
        body = json.dumps({
            "error": f"rate limit exceeded ({self.per_minute}/min); "
                     "retry shortly"}).encode()
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [(b"content-type", b"application/json"),
                        (b"retry-after", b"30")],
        })
        await send({"type": "http.response.body", "body": body})
