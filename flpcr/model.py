"""Campaigns: what a creator funds, and the schedule the puzzle enforces."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import hashlib
from typing import FrozenSet, Optional, Tuple

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
    # Time-weighted holders of an NFT collection, one share per NFT. `target` is
    # the launcher id of the DID that minted the collection: on chain, that DID is
    # the only thing tying the NFTs together. Nothing is staked.
    NFT_HOLD = 3


class Holding(IntEnum):
    # NFTs of one collection, counted per holder. `asset` is the collection
    # id: the launcher id of the DID that minted the collection (see SPEC
    # section 6.8 for why not the marketplace "collection id").
    NFT = 1
    # Mojos of one CAT. `asset` is the CAT's asset id.
    CAT = 2


MAX_BOOST_CAP_BPS = 100_000  # 10x
# Default fee window (Params.fee_window): one day. Long enough that a flash
# deposit is worth a negligible share of a swap's fee weight, short enough that
# the fee half of the budget is credited (at each bucket's end) within a day.
DEFAULT_FEE_WINDOW = BLOCKS_PER_DAY
# Chia's burn address: Forge pools burn their locked LP floor to it. Never a
# position; no campaign pays it (claim.rue refuses it too).
BURN_PUZZLE_HASH = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000dead")
# The most addresses a creator can exclude from one campaign (create.rue MAX_EXCLUDED).
MAX_EXCLUDED = 16


@dataclass(frozen=True)
class Requirement:
    """Holding at least `min_count` of something, checked block by block."""

    holding: Holding
    asset: bytes
    min_count: int = 1

    def __post_init__(self) -> None:
        if self.min_count < 1:
            raise ValueError("min_count must be at least 1")

    def as_clvm(self) -> list:
        return [int(self.holding), self.asset, self.min_count]


@dataclass(frozen=True)
class Boost:
    """While `requirement` is met, the holder's weight is raised by `bps`
    (2,000 = +20%). Boosts stack up to the campaign's cap, so tiers are boosts
    on the same collection at rising counts."""

    requirement: Requirement
    bps: int

    def __post_init__(self) -> None:
        if self.bps <= 0:
            raise ValueError("a boost must add something")

    def as_clvm(self) -> list:
        return [*self.requirement.as_clvm(), self.bps]


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
    # A holder earns only while meeting every gate (e.g. holding a Wizard NFT).
    gates: Tuple[Requirement, ...] = ()
    # Weight multipliers while a requirement is met, stacking up to the cap.
    boosts: Tuple[Boost, ...] = ()
    boost_cap_bps: int = 25_000  # 2.5x, Merkl's usual ceiling
    # Fee weighting (SPEC section 6.3): the length, in blocks, of both the
    # campaign-relative buckets the fee budget is split in and the lookback
    # over which an LP's share is averaged when a swap pays a fee. A one-block
    # deposit counts for at most 1/fee_window of one swap's weight.
    fee_window: int = DEFAULT_FEE_WINDOW
    # NFT_HOLD on a col1 collection: (minting DID launcher id, the metadata's collection id).
    # Committed by the params hash and revealed by the create spend, so anyone (a service
    # booting from chain, a disputer) can check membership; target must be its col1 id.
    collection: Optional[Tuple[bytes, str]] = None

    def __post_init__(self) -> None:
        if self.collection is not None:
            did, uuid = self.collection
            if len(did) != 32 or not isinstance(uuid, str) or not uuid:
                raise ValueError("collection is (32-byte minting DID launcher id, metadata collection id)")
        if not BPS <= self.boost_cap_bps <= MAX_BOOST_CAP_BPS:
            raise ValueError("boost_cap_bps must be between 10000 (1x) and 100000 (10x)")
        if isinstance(self.fee_window, bool) or not isinstance(self.fee_window, int) or self.fee_window < 1:
            raise ValueError("fee_window must be a whole number of blocks, at least 1")

    def multiplier_bps(self, satisfied: Tuple[bool, ...]) -> int:
        """Weight multiplier for a holder meeting the boosts flagged in `satisfied`."""
        raw = BPS + sum(b.bps for b, ok in zip(self.boosts, satisfied) if ok)
        return min(raw, self.boost_cap_bps)

    def as_clvm(self) -> list:
        # The first two fields are the only ones the create puzzle reads; the
        # rest are committed by the hash and applied by the engine.
        return [
            self.time_bps,
            self.fees_bps,
            self.min_balance,
            sorted(self.blacklist),
            sorted(self.whitelist),
            1 if self.forward else 0,
            [g.as_clvm() for g in self.gates],
            [b.as_clvm() for b in self.boosts],
            self.boost_cap_bps,
            self.fee_window,
            # Appended only when set, so campaigns without one keep their ids.
            *([[self.collection[0], self.collection[1].encode()]] if self.collection else []),
        ]

    def hash(self) -> bytes:
        return sha256tree(self.as_clvm())

    @classmethod
    def from_clvm(cls, v: list) -> "Params":
        """The inverse of `as_clvm` for params without gates or boosts (what a
        deploy record stores), so a campaign rebuilt from its record keeps its
        blacklist and hashes to the same id."""
        if v[6] or v[7]:
            raise ValueError("gates and boosts are not rebuilt from a record yet")
        h = lambda x: bytes.fromhex(x) if isinstance(x, str) else bytes(x)  # noqa: E731
        collection = None
        if len(v) > 10 and v[10]:
            did, uuid = v[10]
            collection = (h(did), uuid if isinstance(uuid, str) else bytes(uuid).decode())
        return cls(time_bps=v[0], fees_bps=v[1], min_balance=v[2], blacklist=frozenset(h(x) for x in v[3]),
                   whitelist=frozenset(h(x) for x in v[4]), forward=bool(v[5]), boost_cap_bps=v[8], fee_window=v[9],
                   collection=collection)


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
        if p.collection is not None:
            if self.kind != Kind.NFT_HOLD:
                raise ValueError("only an NFT_HOLD campaign names a collection")
            did, uuid = p.collection
            if hashlib.sha256((did.hex() + uuid).encode()).digest() != self.target:
                raise ValueError("an NFT campaign naming a collection must target its col1 id (sha256 of DID hex + id)")
        if self.kind in (Kind.CAT_HOLD, Kind.NFT_HOLD) and p.fees_bps:
            raise ValueError(f"fee weighting needs a pool; {self.kind.name} is time only")
        if p.min_balance < 0:
            raise ValueError("min_balance must be >= 0")
        if p.blacklist & p.whitelist:
            raise ValueError("a puzzle hash cannot be on both lists")
        if len(p.blacklist) > MAX_EXCLUDED:
            raise ValueError(f"at most {MAX_EXCLUDED} excluded addresses")
        if any(len(x) != 32 for x in p.blacklist):
            raise ValueError("an excluded address is a 32-byte puzzle hash")

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
        if puzzle_hash == BURN_PUZZLE_HASH or puzzle_hash in p.blacklist:
            return False
        if p.whitelist and puzzle_hash not in p.whitelist:
            return False
        return balance > 0 and balance >= p.min_balance
