"""The singleton lease: one create at a time holds the distributor's next generation.

A creator's consent is a mode-23 message naming the singleton's *current* coin id, so
it goes stale the moment anything else spends the singleton. The create intake
(`flpcr.service.intake`) therefore takes a lease when it quotes: until the lease
lapses (`LEASE_SECONDS`) or the create it guards has landed, the service's other
writers leave the singleton alone. Each of them checks `busy()` once, under the
service lock, before it builds a spend:

* `CampaignService.operator_step` (posting a root) returns None while busy;
* `ClaimSequencer.run_batch` returns None while busy; its queue waits for the next batch.

A lease is either *open* (quoted, waiting for the creator's signed bundle; it lapses at
`expires_at`) or *pinned* (the create has been pushed; it holds until the intake
releases it, whether the push confirmed or failed). Only one lease exists at a time.
The lease is in-process state: it orders the writers inside one service, it is not a
chain rule, and another builder spending the same distributor ignores it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

LEASE_SECONDS = 120


class LeaseBusy(ValueError):
    """Another create holds the singleton; retry after `retry_after` seconds."""

    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class Lease:
    holder: str  # the quote id
    coin_id: bytes  # the singleton coin the creator's message names
    expires_at: float
    pinned: bool = False  # pushed: holds until released, whatever the clock says


class SingletonLease:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._lease: Optional[Lease] = None
        self._lock = threading.Lock()

    def _live(self) -> Optional[Lease]:
        lease = self._lease
        if lease is not None and not lease.pinned and self.clock() >= lease.expires_at:
            self._lease = lease = None
        return lease

    def busy(self) -> bool:
        """True while a create holds the singleton: other writers must not spend it."""
        with self._lock:
            return self._live() is not None

    def take(self, holder: str, coin_id: bytes, seconds: float = LEASE_SECONDS) -> Lease:
        with self._lock:
            live = self._live()
            if live is not None and live.holder != holder:
                wait = 30 if live.pinned else max(1, int(live.expires_at - self.clock()) + 1)
                raise LeaseBusy("another campaign create holds the distributor; retry shortly", wait)
            self._lease = Lease(holder, coin_id, self.clock() + seconds)
            return self._lease

    def held_by(self, holder: str) -> Optional[Lease]:
        """The lease if `holder` holds it and it has not lapsed."""
        with self._lock:
            live = self._live()
            return live if live is not None and live.holder == holder else None

    def pin(self, holder: str) -> None:
        with self._lock:
            live = self._live()
            if live is None or live.holder != holder:
                raise ValueError("the lease is not held by this quote")
            live.pinned = True

    def release(self, holder: str) -> None:
        with self._lock:
            if self._lease is not None and self._lease.holder == holder:
                self._lease = None
