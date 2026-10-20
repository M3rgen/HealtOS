"""Browser hardening headers for every HTTP response."""

from __future__ import annotations

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.host_validation import LOCAL_HOSTS, hostname

# The built dashboard (vite) uses only same-origin module scripts and stylesheets.
# - 'wasm-unsafe-eval': the meshopt GLB decoder compiles WebAssembly (no JS eval).
# - style-src-attr: chart SVG markup carries style="stop-color:…"; <style> stays blocked.
# - blob: in img-src/connect-src: appearance uploads and GLTFLoader textures are blob:
#   URLs, and three.js ImageBitmapLoader fetch()es them. data: covers CSS-inlined SVGs.
# - worker-src blob: lets three.js decoders run in workers if they are enabled.
# - connect-src adds the exact ws:// authority of a loopback Host, so the Umay socket
#   works on any tunnel port even in browsers whose 'self' does not cover ws:.
APP_POLICY = (
    "default-src 'self'",
    "script-src 'self' 'wasm-unsafe-eval'",
    "style-src 'self'",
    "style-src-attr 'unsafe-inline'",
    "img-src 'self' blob: data:",
    "font-src 'self' data:",  # Inter is self-hosted; data: in case vite inlines a subset
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
)
# FastAPI's interactive docs are a developer tool built from the jsDelivr CDN and an
# inline bootstrap script; keep that exception off the dashboard itself.
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})
DOCS_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "img-src 'self' data: https://fastapi.tiangolo.com",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)


def app_policy(host: str | None) -> str:
    connect = "connect-src 'self' blob:"
    name = hostname(host) if host else None
    if host and name in LOCAL_HOSTS and not name.startswith("["):  # CSP has no IPv6 host-source
        connect += f" ws://{host.lower()}"
    return "; ".join((*APP_POLICY, connect))


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] in DOCS_PATHS:
            policy = DOCS_POLICY
        else:
            hosts = Headers(scope=scope).getlist("host")
            policy = app_policy(hosts[0] if len(hosts) == 1 else None)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", policy)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                # Not "no-referrer": under it the Fetch spec sends "Origin: null" on
                # same-origin POSTs (Chromium does for the login form), which would
                # defeat the Origin-based CSRF checks. same-origin never leaks off-site.
                headers.setdefault("Referrer-Policy", "same-origin")
            await send(message)

        await self.app(scope, receive, send_with_headers)
