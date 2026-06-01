from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ..models import Destination, PingSample


class Probe(ABC):
    """Long-running probe yielding one PingSample per scheduled attempt."""

    def __init__(self, dest: Destination) -> None:
        self.dest = dest

    @abstractmethod
    def run(self) -> AsyncIterator[PingSample]:
        """Async iterator producing PingSamples until cancelled."""

    @abstractmethod
    async def probe_once(self) -> PingSample:
        """One-shot manual probe, used by the Settings UI live-test."""
