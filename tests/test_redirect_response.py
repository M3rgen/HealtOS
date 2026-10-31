"""Malformed redirect targets cannot take a received stream away from the archive."""

import gzip
import hashlib

import httpx
import pytest
from raw_response_records import FlowingBody, client, no_secret, stored

from app.google_health.client import GoogleHealthError


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("target", ["example.invalid:bad", "[invalid-ipv6]"])
@pytest.mark.parametrize("follow", [False, True])
@pytest.mark.parametrize("encoding", ["identity", "gzip"])
async def test_redirect_stream_is_closed_and_archived_before_safe_rejection(
    sessions, caplog, status, target, follow, encoding
):
    payload = b'  {"dataPoints": [], "private-marker": "synthetic"}\n'
    wire = gzip.compress(payload) if encoding == "gzip" else payload
    calls, streams = [], []

    def handler(request):
        calls.append(request)
        if request.url.host == "example.invalid":
            return httpx.Response(302, headers={"location": "/unrelated-target"})
        stream = FlowingBody(wire, finish=True)
        stream.release.set()
        streams.append(stream)
        return httpx.Response(
            status,
            stream=stream,
            headers={
                "location": f"https://{target}/private-marker",
                "content-encoding": encoding,
                "content-length": str(len(wire)),
            },
        )

    google = client(handler, sessions)
    google.http.follow_redirects = follow
    hooks_before = len(google.http.event_hooks["response"])
    try:
        for _ in range(2):
            with pytest.raises(GoogleHealthError) as caught:
                await google.get_identity()
            assert str(caught.value) == f"Google Health API returned HTTP {status}"
            assert caught.value.__context__ is None and caught.value.__cause__ is None
        assert len(calls) == 2 and len(google.http.event_hooks["response"]) == hooks_before + 1
        assert all(s.close_calls == 1 and s.closed.is_set() for s in streams)
        rows = await stored(sessions)
        assert len(rows) == 2
        for row in rows:
            assert row.status_code == status and row.body == wire and row.attempt == 0
            assert row.complete and row.error is None and row.body_size == len(wire)
            assert row.body_sha256 == hashlib.sha256(wire).hexdigest()
            assert row.headers == {"content-encoding": encoding, "content-length": str(len(wire))}
            no_secret(row)
        assert "private-marker" not in caplog.text and "secret-access-token" not in caplog.text
        # Unmarked requests on the same injected client still construct next_request.
        response = await google.http.get(
            "https://example.invalid/unrelated", follow_redirects=False
        )
        assert response.next_request.url.path == "/unrelated-target" and len(calls) == 3
    finally:
        await google.http.aclose()
