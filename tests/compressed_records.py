"""Synthetic wire entities shared by compression and retrieval regressions."""

import gzip
import zlib


def compressed(payload, encoding, damage="valid"):
    if damage == "stream":
        return compressed(payload, encoding) + compressed(b"private-marker", encoding)
    if damage in {"junk", "zero"}:
        return compressed(payload, encoding) + {"junk": b"private-marker", "zero": b"\0"}[damage]
    if encoding == "identity":
        return payload
    if encoding == "gzip":
        wire, trailer = gzip.compress(payload, mtime=0), 8
    elif encoding == "deflate":
        wire, trailer = zlib.compress(payload), 4
    else:
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        return compressor.compress(payload) + compressor.flush(
            zlib.Z_SYNC_FLUSH if damage == "truncated" else zlib.Z_FINISH
        )
    if damage == "truncated":
        return wire[:-trailer]
    if damage == "checksum":
        return wire[:-trailer] + bytes([wire[-trailer] ^ 1]) + wire[-trailer + 1 :]
    return wire
