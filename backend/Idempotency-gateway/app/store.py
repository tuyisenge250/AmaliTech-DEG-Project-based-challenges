"""In-memory idempotency store.

Each entry maps an ``Idempotency-Key`` to the request fingerprint it was first
used with and the asyncio task that is (or was) processing that request.
Because the *task* is stored rather than just the final result, a duplicate that
arrives while the first request is still running can simply await the same task.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StoredResponse:
    """The response sent for the first request, replayed verbatim for duplicates."""

    status_code: int
    body: dict[str, Any]


@dataclass
class Entry:
    fingerprint: str
    task: asyncio.Task[StoredResponse]
    expires_at: float


class IdempotencyStore:
    def __init__(
        self,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, Entry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str) -> Entry | None:
        entry = self._entries.get(key)
        if entry is not None and self._is_expired(entry):
            del self._entries[key]
            return None
        return entry

    def put(self, key: str, fingerprint: str, task: asyncio.Task[StoredResponse]) -> Entry:
        entry = Entry(fingerprint, task, expires_at=self._clock() + self._ttl)
        self._entries[key] = entry
        return entry

    def discard(self, key: str, task: asyncio.Task[StoredResponse] | None = None) -> None:
        """Remove ``key``. If ``task`` is given, only remove it if it still owns the key."""
        entry = self._entries.get(key)
        if entry is not None and (task is None or entry.task is task):
            del self._entries[key]

    def purge_expired(self) -> int:
        expired = [k for k, e in self._entries.items() if self._is_expired(e)]
        for key in expired:
            del self._entries[key]
        return len(expired)

    def _is_expired(self, entry: Entry) -> bool:
        # Never evict a request that is still being processed: a waiter may be
        # blocked on it and the charge has not completed yet.
        return entry.task.done() and self._clock() >= entry.expires_at
