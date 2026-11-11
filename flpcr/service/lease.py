"""The singleton lease: a quoted create asks the service's other writers to wait for it.

A creator's consent is a mode-23 message naming the singleton's *current* coin id, so
it goes stale the moment anything else spends the singleton. The create intake
(`flpcr.service.intake`) therefore takes a lease when it quotes, and the service's other
writers check `busy()` once, under the service lock, before they build a spend:

* `CampaignService.operator_step` (posting a root) returns None while busy;
* `ClaimSequencer.run_batch` returns None while busy; its queue waits for the next batch.

Any new spend path in the service must check `busy()` the same way, under the service lock.

A quote is free and unsigned, so a lease is a courtesy with a hard limit, never a hold
(audit 2026-11-10, CS-01: quoting every two minutes stopped roots, claims and every
other creator, at no cost):

* *Open* leases (quoted, waiting for the creator's signed bundle) are shared. Any number of
  quotes may be open on the same singleton coin; none blocks another. The first signed
  create to arrive wins, and the others are told to re-quote when they find the coin spent.
* Open leases defer the writers for at most `MAX_DEFER` seconds in a row. Once quotes have
  kept them waiting that long, `busy()` answers False until a moment with no open lease,
  so an endless stream of quotes costs one delay and then nothing. A writer that spends
  the singleton makes the open quotes stale: their creators re-quote.
* A *pinned* lease (a create has been pushed and is waiting to confirm) is exclusive and
  holds until the intake releases it, whether the push confirmed or failed.
  `CampaignService.follow` stands down only for a pinned lease: the pushed create is the
  intake's own spend, and the intake applies it or boots.

The lease is in-process state: it orders the writers inside one service, it is not a
chain rule, and another builder spending the same distributor ignores it
(`scripts/deploy_testnet.py` must not run against a distributor the service operates).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

LEASE_SECONDS = 120
MAX_DEFER = 300  # the longest open quotes may keep the operator and the sequencer waiting
MAX_OPEN = 64  # open quotes at once; more are asked to retry


class LeaseBusy(ValueError):
    """The distributor cannot take this now; retry after `retry_after` seconds."""

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
    def __init__(self, clock: Callable[[], float] = time.time, max_defer: float = MAX_DEFER):
        self.clock = clock
        self.max_defer = max_defer
        self._open: Dict[str, Lease] = {}
        self._pinned: Optional[Lease] = None
        self._deferring_since: Optional[float] = None
        self._lock = threading.Lock()

    def _prune(self) -> None:
        now = self.clock()
        for k in [k for k, v in self._open.items() if now >= v.expires_at]:
            del self._open[k]

    def pinned(self) -> bool:
        """True while a pushed create is waiting to confirm."""
        with self._lock:
            return self._pinned is not None

    def busy(self) -> bool:
        """True while the writers should leave the singleton alone: a create is pinned, or
        quotes are open and have not yet kept the writers waiting `max_defer` seconds."""
        with self._lock:
            if self._pinned is not None:
                return True
            self._prune()
            if not self._open:
                self._deferring_since = None
                return False
            now = self.clock()
            if self._deferring_since is None:
                self._deferring_since = now
            return now - self._deferring_since < self.max_defer

    def take(self, holder: str, coin_id: bytes, seconds: float = LEASE_SECONDS) -> Lease:
        with self._lock:
            if self._pinned is not None and self._pinned.holder != holder:
                raise LeaseBusy("another campaign create is being confirmed; retry shortly", 30)
            self._prune()
            if holder not in self._open and len(self._open) >= MAX_OPEN:
                wait = max(1, int(min(v.expires_at for v in self._open.values()) - self.clock()) + 1)
                raise LeaseBusy("too many campaign quotes are open; retry shortly", wait)
            lease = self._open[holder] = Lease(holder, coin_id, self.clock() + seconds)
            return lease

    def held_by(self, holder: str) -> Optional[Lease]:
        """The lease if `holder` holds it and it has not lapsed."""
        with self._lock:
            if self._pinned is not None and self._pinned.holder == holder:
                return self._pinned
            self._prune()
            return self._open.get(holder)

    def pin(self, holder: str) -> None:
        with self._lock:
            self._prune()
            if self._pinned is not None and self._pinned.holder != holder:
                raise LeaseBusy("another campaign create is being confirmed; retry shortly", 30)
            lease = self._open.pop(holder, None) or self._pinned
            if lease is None:
                raise ValueError("the lease is not held by this quote")
            lease.pinned = True
            self._pinned = lease

    def release(self, holder: str) -> None:
        with self._lock:
            self._open.pop(holder, None)
            if self._pinned is not None and self._pinned.holder == holder:
                self._pinned = None
