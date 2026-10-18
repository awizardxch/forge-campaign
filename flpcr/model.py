"""Campaigns: what a creator funds, and the schedule the puzzle enforces."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import FrozenSet, Optional

from .clvm import sha256tree

CAMPAIGN_TAG = "flpcr-campaign-v1"
BPS = 10_000
# Creation fee, taken out of the deposit as Merkl does: a creator who deposits
# 1,000 funds a 970 budget. The distributor curries the same number.
DEFAULT_FEE_BPS = 300
BLOCKS_PER_DAY = 4608  # 18.75 s per block
# A convenience for tests and fixtures; the interface makes the creator choose.
DEFAULT_CLAIM_WINDOW = 7 * BLOCKS_PER_DAY


class Kind(IntEnum):
    # Time-weighted holders of a Forge pool's LP CAT. `target` is the pool's launcher id.
    LP_HOLD = 1
    # Time-weighted holders of any CAT. `target` is the CAT's asset id (TAIL hash).
    CAT_HOLD = 2


@dataclass(frozen=True)
class Params:
    """Scoring rules. Committed on chain by `params_hash`; published in full in the
    creation spend's solution so anyone can recover them."""

    # Split of each block's emission between share-of-supply and share-of-fees.
    # Fee weighting only means something for LP_HOLD; CAT_HOLD must be all time.
    time_bps: int = BPS
    fees_bps: int = 0
    # A holder below this balance (mojos of the tracked asset) is not eligible.
    min_balance: int = 0
    blacklist: FrozenSet[bytes] = frozenset()
    # Empty means everyone not blacklisted.
    whitelist: FrozenSet[bytes] = frozenset()
    # Credit balances held by known wrappers (a vault pool's nested LP reserve)
    # to the wrapper's own holders instead of to the wrapper.
    forward: bool = True

    def as_clvm(self) -> list:
        return [
            self.time_bps,
            self.fees_bps,
            self.min_balance,
            sorted(self.blacklist),
            sorted(self.whitelist),
            1 if self.forward else 0,
        ]

    def hash(self) -> bytes:
        return sha256tree(self.as_clvm())


@dataclass(frozen=True)
class Campaign:
    distributor_id: bytes  # launcher id of the distributor that holds the reward asset
    creator: bytes  # puzzle hash; receives the unallocated remainder after the end
    reward_asset: bytes  # ZERO_32 for XCH, else the CAT asset id
    amount: int  # mojos of reward_asset the creator deposits, fee included
    start: int  # block height, inclusive
    end: int  # block height, exclusive
    kind: Kind
    target: bytes
    params: Params = field(default_factory=Params)
    nonce: int = 0  # lets one creator fund two otherwise identical campaigns
    fee_bps: int = DEFAULT_FEE_BPS  # the distributor's, copied in so the id commits to it
    # Blocks after `end` before the creator may reclaim. Chosen by the creator
    # and committed in the id, so the campaign's terms are complete at creation.
    # Holders can claim past it, right up until a reclaim actually lands.
    claim_window: int = DEFAULT_CLAIM_WINDOW

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("amount must be positive")
        if not 0 <= self.fee_bps < BPS:
            raise ValueError("fee_bps out of range")
        if self.budget <= 0:
            raise ValueError("nothing left to distribute after the fee")
        if self.claim_window <= 0:
            raise ValueError("claim_window must be positive")
        if self.end <= self.start:
            raise ValueError("end must be after start")
        p = self.params
        if p.time_bps < 0 or p.fees_bps < 0 or p.time_bps + p.fees_bps != BPS:
            raise ValueError("time_bps + fees_bps must equal 10000")
        if self.kind == Kind.CAT_HOLD and p.fees_bps:
            raise ValueError("fee weighting needs a pool; CAT_HOLD is time only")
        if p.min_balance < 0:
            raise ValueError("min_balance must be >= 0")
        if p.blacklist & p.whitelist:
            raise ValueError("a puzzle hash cannot be on both lists")

    @property
    def fee(self) -> int:
        return self.amount * self.fee_bps // BPS

    @property
    def budget(self) -> int:
        """What holders can earn: the deposit less the fee."""
        return self.amount - self.fee

    @property
    def reclaim_from(self) -> int:
        """First height at which the creator may reclaim what is left."""
        return self.end + self.claim_window

    @property
    def length(self) -> int:
        return self.end - self.start

    def as_clvm(self) -> list:
        return [
            CAMPAIGN_TAG,
            self.distributor_id,
            self.creator,
            self.reward_asset,
            self.amount,
            self.start,
            self.end,
            int(self.kind),
            self.target,
            self.params.hash(),
            self.nonce,
            self.fee_bps,
            self.claim_window,
        ]

    @property
    def id(self) -> bytes:
        return sha256tree(self.as_clvm())

    def emittable(self, height: int) -> int:
        """Mojos of the budget the schedule has released by `height`. The
        distributor's `post_root` leaf refuses any campaign total above this,
        so no root can hand out more, whatever the engine computes.
        Telescoping floors: emittable(end) == budget exactly."""
        h = min(max(height, self.start), self.end)
        return self.budget * (h - self.start) // self.length

    def active_in(self, a: int, b: int) -> Optional[tuple]:
        """The part of [a, b) this campaign emits in, or None."""
        lo, hi = max(a, self.start), min(b, self.end)
        return (lo, hi) if lo < hi else None

    def eligible(self, puzzle_hash: bytes, balance) -> bool:
        p = self.params
        if puzzle_hash in p.blacklist:
            return False
        if p.whitelist and puzzle_hash not in p.whitelist:
            return False
        return balance > 0 and balance >= p.min_balance
