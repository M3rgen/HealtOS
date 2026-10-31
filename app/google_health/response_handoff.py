"""Take ownership of redirect streams before HTTPX parses their Location header."""

from __future__ import annotations

import httpx


class RedirectResponseReceived(Exception):
    """An internal handoff, never a transport error or an upstream error message."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__("Google Health redirect response received")
        self.response = response


async def own_redirect(response: httpx.Response) -> None:
    """A public response hook, scoped to the data client's marked requests.

    Even with follow_redirects=False, HTTPX constructs next_request after hooks.
    Transfer the untouched headers and stream to our reader before that happens.
    HTTPX closes its response when a hook raises: leave it an empty stream so
    that cleanup cannot read, close or stall the stream we now own. The existing
    reader applies the body, close and cancellation budgets to that stream once.
    Other requests on an injected/shared client retain their normal behavior.
    """
    if not response.request.extensions.get("healthos_capture_redirect"):
        return
    if not response.has_redirect_location:
        return
    owned = httpx.Response(
        response.status_code,
        headers=response.headers,
        stream=response.stream,
        request=response.request,
        extensions=response.extensions,
        default_encoding=response.default_encoding,
    )
    response.stream = httpx.ByteStream(b"")
    raise RedirectResponseReceived(owned)
