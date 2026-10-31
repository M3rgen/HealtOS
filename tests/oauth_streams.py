"""Real, observable token streams with synthetic credentials only."""

import asyncio

import httpx

LIMIT = 1024 * 1024
PAYLOAD = b'{"access_token":"synthetic-private-access","refresh_token":"synthetic-private-refresh"}'


class TokenStream(httpx.AsyncByteStream):
    def __init__(
        self, chunks, *, read_error=None, close_error=None, wait_read=False, wait_close=False
    ):
        self.chunks = chunks
        self.read_error, self.close_error = read_error, close_error
        self.wait_read, self.wait_close = wait_read, wait_close
        self.pulled = self.close_calls = 0
        self.reading, self.closing, self.closed = (asyncio.Event() for _ in range(3))

    async def __aiter__(self):
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk
        self.reading.set()
        if self.wait_read:
            await asyncio.Event().wait()
        if self.read_error:
            raise self.read_error

    async def aclose(self):
        self.close_calls += 1
        self.closing.set()
        try:
            if self.wait_close:
                await asyncio.Event().wait()
            if self.close_error:
                raise self.close_error
        finally:
            self.closed.set()
