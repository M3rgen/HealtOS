"""Self-contained login page that exchanges APP_UI_TOKEN for a session cookie."""

from __future__ import annotations

import base64
import hashlib
from html import escape
from urllib.parse import parse_qs

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.auth import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    LOGIN_PATH,
    cookie_value,
    request_authenticated,
    safe_next,
    same_origin_form,
    token_matches,
)

router = APIRouter(include_in_schema=False)
MAX_FORM_BYTES = 8 * 1024

STYLE = (
    ":root{color-scheme:dark;--page:#181814;--surface:#20201c;--inset:#242420;--line:#373731;"
    "--border:#262421;--text:#f8f8f8;--muted:#b9b7ae;--amber:#f0c478;--ink:#141413;"
    "--error:#ff8a8a}"
    "*{box-sizing:border-box}"
    "html,body{height:100%;margin:0}"
    "body{display:grid;place-items:center;padding:16px;background:var(--page);color:var(--text);"
    'font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}'
    "main{width:min(380px,100%);background:var(--surface);border:1px solid var(--border);"
    "border-radius:12px;padding:28px 26px 24px}"
    "h1{margin:0 0 6px;font-size:28px;font-weight:750;letter-spacing:-1px}"
    "h1 span{font-weight:600;color:var(--amber)}"
    "p{margin:0;color:var(--muted)}"
    "form{display:grid;gap:10px;margin-top:22px}"
    "label{font-size:13px;font-weight:600}"
    "input{width:100%;font:inherit;color:var(--text);background:var(--inset);"
    "border:1px solid var(--line);border-radius:8px;padding:11px 12px}"
    "input:focus-visible,button:focus-visible{outline:2px solid var(--amber);outline-offset:3px}"
    "button{font:inherit;font-weight:650;color:var(--ink);background:var(--amber);border:0;"
    "border-radius:8px;padding:11px 14px;margin-top:6px;cursor:pointer}"
    ".error{color:var(--error);font-size:13px}"
    ".hint{margin-top:16px;font-size:12px}"
    "code{font-size:12px;color:var(--text)}"
    "@media(max-height:460px){main{padding:16px 18px}h1{font-size:22px}form{margin-top:12px;"
    "gap:6px}.hint{display:none}}"
)
STYLE_HASH = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
CSP = (
    f"default-src 'none'; style-src 'sha256-{STYLE_HASH}'; img-src data:; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
ERRORS = {
    "token": "Erişim anahtarı yanlış. Kontrol edip tekrar dene.",
    "origin": "İstek doğrulanamadı. Sayfayı yenileyip tekrar dene.",
}


def page(next_path: str, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    message = f'<p class="error" role="alert">{escape(ERRORS[error])}</p>' if error else ""
    html = f"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#181814">
<meta name="robots" content="noindex">
<link rel="icon" href="data:,">
<title>HealthOS · Giriş</title>
<style>{STYLE}</style>
</head>
<body>
<main>
<h1>Health<span>OS</span></h1>
<p>Panele devam etmek için erişim anahtarını gir.</p>
<form method="post" action="{LOGIN_PATH}">
<input type="hidden" name="next" value="{escape(next_path, quote=True)}">
<label for="token">Erişim anahtarı</label>
<input id="token" name="token" type="password" autocomplete="current-password"
 autocapitalize="off" spellcheck="false" required autofocus>
{message}
<button type="submit">Giriş yap</button>
</form>
<p class="hint">Anahtar, sunucudaki <code>APP_UI_TOKEN</code> değeridir.
Bu tarayıcı seni 30 gün hatırlar.</p>
</main>
</body>
</html>
"""
    return HTMLResponse(
        html,
        status_code=status_code,
        headers={"Content-Security-Policy": CSP, "Cache-Control": "no-store"},
    )


def _token(request: Request) -> str | None:
    return request.app.state.settings.app_ui_token


@router.get(LOGIN_PATH)
async def login_page(
    request: Request, next_path: str | None = Query(None, alias="next")
) -> Response:
    target = safe_next(next_path)
    if request_authenticated(_token(request), request.headers):
        return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})
    return page(target)


@router.post(LOGIN_PATH)
async def login(request: Request) -> Response:
    token = _token(request)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return Response("Form too large", status_code=413)
    try:
        form = parse_qs(body.decode(), max_num_fields=8)
    except (UnicodeDecodeError, ValueError):
        form = {}
    target = safe_next((form.get("next") or ["/"])[0])
    if not same_origin_form(request.headers, request.url.scheme):
        return page(target, "origin", 403)
    if not token:
        return RedirectResponse(target, status_code=303)
    if not token_matches(token, (form.get("token") or [""])[0]):
        return page(target, "token", 401)
    response = RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        COOKIE_NAME,
        cookie_value(token),
        max_age=COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="strict",
        secure=False,
    )
    return response
