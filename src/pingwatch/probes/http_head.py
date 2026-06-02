from __future__ import annotations

import time

import httpx

from ..models import Destination, PingSample
from .base import Probe


def _normalize_url(address: str) -> str:
    if address.startswith(("http://", "https://")):
        return address
    return f"https://{address}"


class HttpHeadProbe(Probe):
    def __init__(self, dest: Destination) -> None:
        super().__init__(dest)
        self._url = _normalize_url(dest.address)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.dest.timeout_ms / 1000.0,
                follow_redirects=True,
            )
        return self._client

    async def probe_once(self) -> PingSample:
        seq = self._next_seq()
        start_ms = int(time.time() * 1000)
        start = time.monotonic()
        client = self._get_client()
        try:
            resp = await client.head(self._url)
            latency_us = int((time.monotonic() - start) * 1_000_000)
            return PingSample(
                dest_id=self.dest.id,
                ts_ms=start_ms,
                success=resp.status_code < 500,
                latency_us=latency_us,
                sequence=seq,
                error_kind=None if resp.status_code < 500 else f"http_{resp.status_code}",
            )
        except Exception as exc:  # noqa: BLE001
            return PingSample(
                dest_id=self.dest.id,
                ts_ms=start_ms,
                success=False,
                sequence=seq,
                error_kind=type(exc).__name__,
            )

    async def cleanup(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
