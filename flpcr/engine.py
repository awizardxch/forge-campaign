"""The reward engine: chain data in, one cumulative claim tree out.

Pure and deterministic. Every quantity is an exact rational until a holder's
cumulative accrual is floored, so two implementations that follow SPEC.md §6
produce the same root, and anyone can replay a posted root and dispute it.

Claims work like Merkl's: each root lists, per (campaign, holder), everything
the holder has earned since the campaign started. The distributor pays
`cumulative - claimed` whenever the holder chooses to claim, so a holder can
claim after every root, once at the end, or anything between. Because each
cumulative is a floor of a share of whole-mojo segment budgets, a campaign's
cumulatives never sum past `emittable(end)`, the ceiling the puzzle enforces.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Tuple

from .merkle import Proof, build_tree, claim_leaf
from .model import BPS, Campaign, Kind
from .sources import HolderEvent, Source

Balances = Dict[bytes, Fraction]


@dataclass
class EngineState:
    distributor_id: bytes
    root_id: int = 0  # the last posted root; the first is 1
    height: Optional[int] = None  # accrual is complete up to here (exclusive)
    accrued: Dict[bytes, Dict[bytes, Fraction]] = field(default_factory=dict)


@dataclass(frozen=True)
class Claim:
    campaign_id: bytes
    puzzle_hash: bytes
    cumulative: int  # everything earned so far; the puzzle pays cumulative - claimed
    leaf: bytes
    proof: Proof


@dataclass
class Root:
    distributor_id: bytes
    root_id: int
    start: int  # accrual window this root added, [start, end)
    end: int
    root: Optional[bytes]  # None when nothing accrued; not posted
    totals: Dict[bytes, int]  # campaign id -> sum of its cumulatives
    claims: List[Claim]

    def claim_for(self, campaign_id: bytes, puzzle_hash: bytes) -> Optional[Claim]:
        for c in self.claims:
            if c.campaign_id == campaign_id and c.puzzle_hash == puzzle_hash:
                return c
        return None


# --- balance timelines -------------------------------------------------------


class Timeline:
    """Balances of one asset as a step function of height."""

    def __init__(self, events: Iterable[HolderEvent]):
        self._steps: List[Tuple[int, bytes, int]] = sorted(
            ((e.height, e.puzzle_hash, e.delta) for e in events), key=lambda s: s[0]
        )

    def at(self, height: int) -> Dict[bytes, int]:
        bal: Dict[bytes, int] = defaultdict(int)
        for h, ph, d in self._steps:
            if h > height:
                break
            bal[ph] += d
        for ph, b in bal.items():
            if b < 0:
                raise ValueError(f"negative balance for {ph.hex()} at {height}")
        return {ph: b for ph, b in bal.items() if b}

    def changes(self, start: int, end: int) -> List[int]:
        return sorted({h for h, _, _ in self._steps if start < h < end})


class Holdings:
    """Effective positions in a tracked asset: system holders removed, wrapper
    holdings passed through to the wrapper's own holders (one level)."""

    def __init__(self, source: Source, asset_id: bytes, before: int, forward: bool):
        self.excluded = source.excluded(asset_id)
        self.main = Timeline(source.holder_events(asset_id, before))
        self.wrappers: Dict[bytes, Tuple[Timeline, set]] = {}
        if forward:
            for wrapper_ph, share_asset in source.forwarders(asset_id).items():
                self.wrappers[wrapper_ph] = (
                    Timeline(source.holder_events(share_asset, before)),
                    source.excluded(share_asset),
                )

    def changes(self, start: int, end: int) -> List[int]:
        hs = set(self.main.changes(start, end))
        for tl, _ in self.wrappers.values():
            hs.update(tl.changes(start, end))
        return sorted(hs)

    def at(self, height: int) -> Balances:
        out: Balances = defaultdict(Fraction)
        for ph, b in self.main.at(height).items():
            if ph in self.excluded:
                continue
            wrapper = self.wrappers.get(ph)
            if wrapper is None:
                out[ph] += b
                continue
            tl, wrapper_excluded = wrapper
            shares = {p: s for p, s in tl.at(height).items() if p not in wrapper_excluded}
            total = sum(shares.values())
            # A wrapper nobody holds a share of earns for no one.
            for p, s in shares.items():
                out[p] += Fraction(b * s, total)
        return dict(out)


def _eligible(campaign: Campaign, balances: Balances) -> Balances:
    return {ph: b for ph, b in balances.items() if campaign.eligible(ph, b)}


# --- one campaign over one window ---------------------------------------------


def accrue(campaign: Campaign, source: Source, start: int, end: int) -> Balances:
    """Exact credit each holder earns from `campaign` in [start, end)."""
    window = campaign.active_in(start, end)
    if window is None:
        return {}
    lo, hi = window
    asset = source.lp_asset(campaign.target) if campaign.kind == Kind.LP_HOLD else campaign.target
    holdings = Holdings(source, asset, hi, campaign.params.forward)

    cuts = [lo, *holdings.changes(lo, hi), hi]
    segments = [(a, b, _eligible(campaign, holdings.at(a))) for a, b in zip(cuts, cuts[1:])]

    p = campaign.params
    fee_points: List[Tuple[int, Balances]] = []
    if p.fees_bps:
        fee_points = [
            (w, _eligible(campaign, holdings.at(h)))
            for h, w in source.fee_events(campaign.target, lo, hi)
            if w > 0
        ]
        fee_points = [(w, bal) for w, bal in fee_points if bal]
    # A window with no fee-paying action spends its fee share on time weighting,
    # so the budget is never stranded because a pool was quiet.
    time_bps = p.time_bps if fee_points else BPS

    credit: Balances = defaultdict(Fraction)
    for a, b, bal in segments:
        total = sum(bal.values())
        if not total:
            continue  # nobody eligible: this budget stays unallocated, reclaimable
        budget = Fraction((campaign.emittable(b) - campaign.emittable(a)) * time_bps, BPS)
        for ph, x in bal.items():
            credit[ph] += budget * x / total

    if fee_points:
        fee_budget = Fraction((campaign.emittable(hi) - campaign.emittable(lo)) * p.fees_bps, BPS)
        weight = sum(w for w, _ in fee_points)
        for w, bal in fee_points:
            total = sum(bal.values())
            for ph, x in bal.items():
                credit[ph] += fee_budget * w * x / (weight * total)
    return dict(credit)


# --- one root for one distributor ----------------------------------------------


def run_epoch(
    state: EngineState,
    campaigns: Iterable[Campaign],
    source: Source,
    end: int,
) -> Tuple[EngineState, Root]:
    """Accrue [state.height, end) and build root `state.root_id + 1`.

    Returns a new state; `state` is not modified. If nothing accrued in the
    window the state is returned unchanged and the root is not to be posted,
    so the next run covers the longer window.
    """
    campaigns = [c for c in campaigns if c.distributor_id == state.distributor_id]
    if not campaigns:
        raise ValueError("no campaigns for this distributor")
    start = state.height if state.height is not None else min(c.start for c in campaigns)
    if end <= start:
        raise ValueError("window must end after it starts")

    accrued = {cid: dict(v) for cid, v in state.accrued.items()}
    changed = False
    for c in campaigns:
        acc = accrued.setdefault(c.id, {})
        for ph, x in accrue(c, source, start, end).items():
            if x:
                acc[ph] = acc.get(ph, Fraction(0)) + x
                changed = True

    root_id = state.root_id + 1
    if not changed:
        return state, Root(state.distributor_id, root_id, start, end, None, {}, [])

    # Only the campaigns passed in: a reclaimed campaign has left the
    # distributor, and a root naming it would be refused.
    live = {c.id for c in campaigns}
    rows = sorted(
        (cid, ph, int(x))  # int() floors a non-negative Fraction
        for cid, by_holder in accrued.items()
        if cid in live
        for ph, x in by_holder.items()
        if int(x) > 0
    )
    totals: Dict[bytes, int] = defaultdict(int)
    for cid, _, amount in rows:
        totals[cid] += amount
    for c in campaigns:
        # The engine's own copy of the puzzle's ceiling check. Failing here is a
        # bug in the engine, never something to post.
        if totals.get(c.id, 0) > c.emittable(end):
            raise AssertionError(f"campaign {c.id.hex()} over the schedule")

    if not rows:
        # Accrual so far is all sub-mojo; nothing claimable yet.
        return state, Root(state.distributor_id, root_id, start, end, None, {}, [])
    leaves = [claim_leaf(state.distributor_id, cid, ph, amount) for cid, ph, amount in rows]
    root, proofs = build_tree(leaves)
    claims = [Claim(cid, ph, amount, leaf, proofs[leaf]) for (cid, ph, amount), leaf in zip(rows, leaves)]

    new_state = EngineState(state.distributor_id, root_id, end, accrued)
    return new_state, Root(state.distributor_id, root_id, start, end, root, dict(totals), claims)
