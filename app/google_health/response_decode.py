"""Decode entities only after receiving and retaining their original wire bytes."""

from __future__ import annotations

import zlib

import httpx

# Bound newly inflated bytes, not the raw archive. 32 MiB leaves about 3 KiB per
# record at the existing 10,000-record page size (sleep/exercise use 25), while
# preventing a tiny compressed response from allocating gigabytes before JSON.
# Each intermediate layer and all members of one gzip layer share this ceiling.
# Wire/JSON storage is separate; this is not a total process-memory guarantee.
MAX_DECODED_BYTES = 32 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024


class ResponseDecodeRejected(ValueError):
    """A deterministic decode rejection; retrying the same entity cannot fix it."""


class ResponseDecodeLimitExceeded(ResponseDecodeRejected):
    def __init__(self) -> None:
        super().__init__("Google Health response exceeded its decoded byte budget")


class UnsupportedResponseEncoding(ResponseDecodeRejected):
    def __init__(self) -> None:
        super().__init__("Google Health response has an unsupported content encoding")


class TrailingEncodedBytes(ResponseDecodeRejected):
    """Bytes after a complete deflate stream: the same entity fails on every retry."""

    def __init__(self) -> None:
        super().__init__("Google Health response has trailing bytes after its encoded content")


def decode(
    wire: bytes,
    headers: httpx.Headers,
    failure: BaseException | None,
    *,
    max_decoded_bytes: int | None = None,
) -> tuple[bytes, BaseException | None]:
    """Validate bounded compression before JSON can become completion evidence.

    HTTP EOF and compression EOF are independent: a decode failure must not change
    capture.complete. An earlier read/close failure, especially cancellation, wins.
    Identity/unencoded bodies retain their existing raw-body behavior: no inflation
    occurs. Decode stacked encodings in reverse order, bounding every output layer.
    """
    if failure is not None:
        return wire, failure
    limit = MAX_DECODED_BYTES if max_decoded_bytes is None else max_decoded_bytes
    body = wire
    try:
        for encoding in reversed(headers.get_list("content-encoding", split_commas=True)):
            encoding = encoding.strip().lower()
            if encoding == "gzip":
                body = _gzip(body, limit)
            elif encoding == "deflate":
                body = _deflate(body, limit)
            elif encoding != "identity":
                # Optional HTTPX codecs (br/zstd) have no shared bounded-output
                # contract. Never fall back to Response(...).content: it inflates
                # before we can check size. Fail closed even if installed later.
                raise UnsupportedResponseEncoding()
        return body, None
    except ResponseDecodeRejected as exc:
        # Return a fresh error so no traceback retains decoder buffers or input.
        return b"", type(exc)()
    except (OSError, EOFError, zlib.error):
        # Do not retain upstream text, bytes, or a decoder exception chain. This
        # safe failure enters the client's existing bounded connection retry path.
        return b"", httpx.DecodingError("Google Health response content decoding failed")


def _inflate(wire: memoryview, wbits: int, body: bytearray, limit: int) -> int:
    """Append bounded output and return consumed input, validating CRC/size/EOF.

    Never call flush(): its length argument is only a buffer hint, not a cap.
    decompress drains pending output with bounded calls, including at exact limit.
    One extra byte distinguishes exact-limit EOF from overflow without producing
    the rest of a bomb. Input slices also bound zlib's unconsumed/unused copies.
    """
    inflater = zlib.decompressobj(wbits)
    offset = 0
    while True:
        chunk = wire[offset : offset + _CHUNK_BYTES]
        remaining = limit - len(body)
        piece = inflater.decompress(chunk, min(_CHUNK_BYTES, remaining + 1))
        if len(piece) > remaining:
            raise ResponseDecodeLimitExceeded()
        body.extend(piece)
        offset += len(chunk)
        if inflater.eof:
            return offset - len(inflater.unused_data)
        offset -= len(inflater.unconsumed_tail)
        if not chunk and not piece:
            raise EOFError


def _gzip(wire: bytes, limit: int) -> bytes:
    """Validate every gzip member, allowing gzip's zero padding between members."""
    if not wire:
        raise EOFError
    body = bytearray()
    view = memoryview(wire)
    offset = 0
    while offset < len(view):
        offset += _inflate(view[offset:], zlib.MAX_WBITS | 16, body, limit)
        while offset < len(view) and view[offset] == 0:
            offset += 1
    return bytes(body)


def _inflate_single(wire: memoryview, wbits: int, body: bytearray, limit: int) -> None:
    """Require one complete stream to account for every byte of this layer.

    _inflate stops at compression EOF, which can precede the end of its input.
    Its consumed count covers both unused bytes in the final chunk and input
    beyond that chunk. Deflate has no concatenated-member or padding contract:
    even zeros and a second valid stream must fail before JSON is interpreted.

    Not a zlib.error: a suffix after a successfully decoded wrapped stream must
    not trigger raw fallback. Keep bounded output and checksum/EOF validation in
    _inflate; no second pass or unbounded flush is needed here.
    """
    consumed = _inflate(wire, wbits, body, limit)
    if consumed != len(wire):
        raise TrailingEncodedBytes()


def _deflate(wire: bytes, limit: int) -> bytes:
    """Accept zlib-wrapped and legacy raw deflate, preserving fallback semantics.

    Only a wrapped zlib.error permits raw fallback. Missing EOF, trailing bytes
    or overflow is final, never a reason to reinterpret the body under a fresh
    output budget. Both formats must validate the entire layer independently;
    the caller applies this same rule at each deflate in a stacked encoding.
    """
    body = bytearray()
    view = memoryview(wire)
    try:
        _inflate_single(view, zlib.MAX_WBITS, body, limit)
    except zlib.error:
        body.clear()
        _inflate_single(view, -zlib.MAX_WBITS, body, limit)
    return bytes(body)
