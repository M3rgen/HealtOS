from __future__ import annotations

import base64
import time
from dataclasses import dataclass

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


class WebhookVerificationError(ValueError):
    pass


@dataclass
class CachedKeyset:
    value: dict
    expires_at: float


class WebhookSignatureVerifier:
    def __init__(self, keyset_url: str, http: httpx.AsyncClient | None = None):
        self.keyset_url = keyset_url
        self.http = http or httpx.AsyncClient(timeout=10)
        self._cache: CachedKeyset | None = None

    async def verify(self, raw_body: bytes, signature_header: str) -> None:
        try:
            signed = base64.b64decode(signature_header, validate=True)
        except (ValueError, TypeError) as exc:
            raise WebhookVerificationError("Invalid webhook signature encoding") from exc
        if len(signed) <= 5 or signed[0] != 1:
            raise WebhookVerificationError("Invalid Tink signature prefix")
        key_id = int.from_bytes(signed[1:5], "big")
        keyset = await self._get_keyset()
        key_entry = _enabled_key(keyset, key_id)
        if key_entry is None and self._cache is not None:
            key_entry = _enabled_key(await self._get_keyset(force=True), key_id)
        if key_entry is None:
            raise WebhookVerificationError("Webhook signing key is not in the official keyset")
        try:
            serialized = base64.b64decode(key_entry["keyData"]["value"], validate=True)
            fields = _protobuf_fields(serialized)
            x = int.from_bytes(fields[3][0], "big")
            y = int.from_bytes(fields[4][0], "big")
            public_key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
            public_key.verify(signed[5:], raw_body, ec.ECDSA(hashes.SHA256()))
        except (KeyError, IndexError, ValueError, InvalidSignature) as exc:
            raise WebhookVerificationError("Webhook signature verification failed") from exc

    async def _get_keyset(self, force: bool = False) -> dict:
        if not force and self._cache and self._cache.expires_at > time.time():
            return self._cache.value
        response = await self.http.get(self.keyset_url)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or not isinstance(value.get("key"), list):
            raise WebhookVerificationError("Official webhook keyset is malformed")
        self._cache = CachedKeyset(value, time.time() + 3600)
        return value


def _enabled_key(keyset: dict, key_id: int) -> dict | None:
    return next(
        (
            item
            for item in keyset.get("key", [])
            if int(item.get("keyId", -1)) == key_id and item.get("status") == "ENABLED"
        ),
        None,
    )


def _protobuf_fields(data: bytes) -> dict[int, list[bytes]]:
    fields: dict[int, list[bytes]] = {}
    cursor = 0
    while cursor < len(data):
        tag, cursor = _varint(data, cursor)
        field_number, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            _, cursor = _varint(data, cursor)
        elif wire_type == 2:
            size, cursor = _varint(data, cursor)
            end = cursor + size
            if end > len(data):
                raise ValueError("Truncated protobuf field")
            fields.setdefault(field_number, []).append(data[cursor:end])
            cursor = end
        else:
            raise ValueError("Unsupported protobuf wire type")
    return fields


def _varint(data: bytes, cursor: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while cursor < len(data) and shift < 70:
        byte = data[cursor]
        cursor += 1
        value |= (byte & 0x7F) << shift
        if byte < 128:
            return value, cursor
        shift += 7
    raise ValueError("Invalid protobuf varint")
