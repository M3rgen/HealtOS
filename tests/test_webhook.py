import base64

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import Settings
from app.google_health.webhook import WebhookSignatureVerifier, WebhookVerificationError

KEYSET_URL = "https://www.gstatic.com/googlehealthapi/webhooks/webhooks_public_keyset.json"


def _field(number, value):
    return bytes([(number << 3) | 2, len(value)]) + value


async def test_webhook_tink_signature_verification():
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    x = numbers.x.to_bytes(32, "big")
    y = numbers.y.to_bytes(32, "big")
    serialized_proto = bytes([8, 0]) + _field(3, x) + _field(4, y)
    key_id = 12345
    keyset = {
        "key": [
            {
                "keyId": key_id,
                "status": "ENABLED",
                "keyData": {"value": base64.b64encode(serialized_proto).decode()},
            }
        ]
    }

    def handler(request):
        return httpx.Response(200, json=keyset)

    body = b'{"data":{"dataType":"steps"}}'
    der = private.sign(body, ec.ECDSA(hashes.SHA256()))
    signed = bytes([1]) + key_id.to_bytes(4, "big") + der
    verifier = WebhookSignatureVerifier(
        KEYSET_URL, httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await verifier.verify(body, base64.b64encode(signed).decode())
    with pytest.raises(WebhookVerificationError):
        await verifier.verify(body + b"x", base64.b64encode(signed).decode())


async def test_webhook_tink_signature_verification_with_batched_body():
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    serialized_proto = (
        bytes([8, 0])
        + _field(3, numbers.x.to_bytes(32, "big"))
        + _field(4, numbers.y.to_bytes(32, "big"))
    )
    key_id = 24680
    keyset = {
        "key": [
            {
                "keyId": key_id,
                "status": "ENABLED",
                "keyData": {"value": base64.b64encode(serialized_proto).decode()},
            }
        ]
    }

    verifier = WebhookSignatureVerifier(
        KEYSET_URL,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=keyset))
        ),
    )
    body = b'[{"data":{"dataType":"steps"}},{"data":{"dataType":"heart-rate"}}]'
    signature = private.sign(body, ec.ECDSA(hashes.SHA256()))
    signed = bytes([1]) + key_id.to_bytes(4, "big") + signature

    await verifier.verify(body, base64.b64encode(signed).decode())
    with pytest.raises(WebhookVerificationError):
        await verifier.verify(body + b" ", base64.b64encode(signed).decode())


async def test_unknown_cached_webhook_key_forces_rotation_refresh():
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    serialized_proto = (
        bytes([8, 0])
        + _field(3, numbers.x.to_bytes(32, "big"))
        + _field(4, numbers.y.to_bytes(32, "big"))
    )
    key_id = 9876
    rotated = {
        "key": [
            {
                "keyId": key_id,
                "status": "ENABLED",
                "keyData": {"value": base64.b64encode(serialized_proto).decode()},
            }
        ]
    }
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"key": []} if calls == 1 else rotated)

    verifier = WebhookSignatureVerifier(
        KEYSET_URL,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await verifier._get_keyset()
    body = b'{"data":{"dataType":"steps"}}'
    signature = private.sign(body, ec.ECDSA(hashes.SHA256()))
    signed = bytes([1]) + key_id.to_bytes(4, "big") + signature

    await verifier.verify(body, base64.b64encode(signed).decode())

    assert calls == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://keys.example.com/keyset.json",
        "http://www.gstatic.com/googlehealthapi/webhooks/webhooks_public_keyset.json",
        "https://www.gstatic.com.example.com/keyset.json",
        "https://www.gstatic.com@example.com/keyset.json",
    ],
)
def test_keyset_url_must_be_pinned_to_google_static_host(url):
    with pytest.raises(ValueError, match="gstatic"):
        Settings(google_health_webhook_public_key_url=url, _env_file=None)
    with pytest.raises(ValueError, match="gstatic"):
        WebhookSignatureVerifier(url)


def test_default_keyset_url_is_accepted():
    settings = Settings(_env_file=None)
    assert settings.google_health_webhook_public_key_url == KEYSET_URL
