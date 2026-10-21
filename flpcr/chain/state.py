"""The distributor's on-chain State, typed.

Field order is shared.rue's: State, Campaign, Root and Rotation are proper
lists in the order their structs declare. `from_program` reads the CLVM value
the singleton holds; `to_value` is the Python value `to_program` turns back
into it, so `ChainState.from_program(p).to_value()` round-trips.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from chia_wallet_sdk import Program

from .values import to_bytes, to_int, to_list


@dataclass(frozen=True)
class ChainCampaign:
    id: bytes
    creator: bytes
    budget: int
    start: int
    end: int
    claim_window: int
    claimed: int
    excluded: bytes = b""  # tree hash of the creator's excluded list (empty before the field existed)

    def to_value(self) -> list:
        out = [self.id, self.creator, self.budget, self.start, self.end, self.claim_window, self.claimed]
        return out + [self.excluded] if self.excluded else out

    @classmethod
    def from_program(cls, p: Program) -> "ChainCampaign":
        v = to_list(p)
        excluded = to_bytes(v[7]) if len(v) > 7 else b""
        return cls(to_bytes(v[0]), to_bytes(v[1]), *(to_int(x) for x in v[2:7]), excluded)


@dataclass(frozen=True)
class ChainRoot:
    root_id: int
    start: int
    end: int
    root: bytes
    totals: Tuple[Tuple[bytes, int], ...]  # (campaign id, total), ascending by id
    activates_at: int

    def total_for(self, campaign_id: bytes) -> int:
        return dict(self.totals).get(campaign_id, 0)

    def to_value(self) -> list:
        return [self.root_id, self.start, self.end, self.root, [(k, v) for k, v in self.totals], self.activates_at]

    @classmethod
    def from_program(cls, p: Program) -> "ChainRoot":
        v = to_list(p)
        totals = tuple((to_bytes(t.first()), to_int(t.rest())) for t in to_list(v[4]))
        return cls(to_int(v[0]), to_int(v[1]), to_int(v[2]), to_bytes(v[3]), totals, to_int(v[5]))


@dataclass(frozen=True)
class ChainRotation:
    engine_ph: bytes
    effective_at: int

    def to_value(self) -> list:
        return [self.engine_ph, self.effective_at]

    @classmethod
    def from_program(cls, p: Program) -> "ChainRotation":
        v = to_list(p)
        return cls(to_bytes(v[0]), to_int(v[1]))


@dataclass(frozen=True)
class ChainDispute:
    root_id: int
    disputer_ph: bytes
    bond_parent: bytes

    def to_value(self) -> list:
        return [self.root_id, self.disputer_ph, self.bond_parent]

    @classmethod
    def from_program(cls, p: Program) -> "ChainDispute":
        v = to_list(p)
        return cls(to_int(v[0]), to_bytes(v[1]), to_bytes(v[2]))


def _optional(cls, p: Program):
    return None if p.is_null() else cls.from_program(p)


@dataclass(frozen=True)
class ChainState:
    reserve: int
    engine_ph: bytes
    initialized: int
    campaigns: Tuple[ChainCampaign, ...]  # newest first, as `create` prepends
    active: Optional[ChainRoot] = None
    pending: Optional[ChainRoot] = None
    rotation: Optional[ChainRotation] = None
    dispute: Optional[ChainDispute] = None

    @classmethod
    def genesis(cls, engine_ph: bytes) -> "ChainState":
        return cls(0, engine_ph, 0, ())

    def campaign(self, campaign_id: bytes) -> Optional[ChainCampaign]:
        return next((c for c in self.campaigns if c.id == campaign_id), None)

    @property
    def by_id(self) -> Dict[bytes, ChainCampaign]:
        return {c.id: c for c in self.campaigns}

    @property
    def owed(self) -> int:
        """What the reserve must hold: every campaign's unclaimed budget."""
        return sum(c.budget - c.claimed for c in self.campaigns)

    def to_value(self) -> list:
        return [
            self.reserve,
            self.engine_ph,
            self.initialized,
            [c.to_value() for c in self.campaigns] or None,
            self.active.to_value() if self.active else None,
            self.pending.to_value() if self.pending else None,
            self.rotation.to_value() if self.rotation else None,
            self.dispute.to_value() if self.dispute else None,
        ]

    @classmethod
    def from_program(cls, p: Program) -> "ChainState":
        f = to_list(p)
        return cls(
            reserve=to_int(f[0]),
            engine_ph=to_bytes(f[1]),
            initialized=to_int(f[2]),
            campaigns=tuple(ChainCampaign.from_program(c) for c in to_list(f[3])),
            active=_optional(ChainRoot, f[4]),
            pending=_optional(ChainRoot, f[5]),
            rotation=_optional(ChainRotation, f[6]),
            # a pre-dispute revision (seven fields) reads as undisputed
            dispute=_optional(ChainDispute, f[7]) if len(f) > 7 else None,
        )
