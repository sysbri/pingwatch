from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator

import httpx
import structlog

from ..models import Destination, PingSample
from .base import Probe

log = structlog.get_logger(__name__)


def _normalize_url(address: str) -> str:
    if address.startswith(("http://", "https://")):
        return address
    return f"https://{address}"


class HttpHeadProbe(Probe):
    def __init__(self, dest: Destination) -> None:
        super().__init__(dest)
        self._sequence = 0
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
        self._sequence += 1
        seq = self._sequence
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

    async def run(self) -> AsyncIterator[PingSample]:
        await asyncio.sleep(random.uniform(0.0, self.dest.interval_ms / 1000.0))  # noqa: S311  # non-cryptographic jitter/sampling
        try:
            while True:
                t0 = time.monotonic()
                yield await self.probe_once()
                elapsed = time.monotonic() - t0
                interval_s = self.dest.interval_ms / 1000.0
                sleep_for = interval_s - elapsed
                if sleep_for > 0:
                    jitter = random.uniform(-0.05, 0.05) * interval_s  # noqa: S311  # non-cryptographic jitter/sampling
                    await asyncio.sleep(max(0.0, sleep_for + jitter))
        finally:
            if self._client is not None:
                await self._client.aclose()
                self._client = None
