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

Where the operator cuts roots never changes what anyone earns: time credit is
additive over any split of a segment, and the fee share is settled per fixed,
campaign-relative bucket once the bucket has closed (SPEC §6.3). So the exact
accrual at a height depends only on the chain up to that height.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Tuple

from .links import EMPTY_LOG_HASH, Link, LinkBook, check_log, log_hash
from .merkle import Proof, build_tree, claim_leaf
from .model import BPS, Campaign, Holding, Kind, Requirement
from .sources import HolderEvent, Source, fee_weight

Balances = Dict[bytes, Fraction]


@dataclass
class EngineState:
    distributor_id: bytes
    root_id: int = 0  # the last posted root; the first is 1
    height: Optional[int] = None  # accrual is complete up to here (exclusive)
    accrued: Dict[bytes, Dict[bytes, Fraction]] = field(default_factory=dict)
    # The link log prefix the last posted root committed (SPEC §6.8).
    link_count: int = 0
    links_hash: bytes = EMPTY_LOG_HASH


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
    link_count: int = 0  # the link log prefix (heights < end) this root used
    links_hash: bytes = EMPTY_LOG_HASH

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
    holdings passed through to the wrapper's own holders (one level). A
    blacklisted wrapper is not forwarded: its balance stays with the wrapper,
    which the blacklist then keeps from earning."""

    def __init__(self, source: Source, asset_id: bytes, before: int, forward: bool, blacklist=frozenset()):
        self.excluded = source.excluded(asset_id)
        self.main = Timeline(source.holder_events(asset_id, before))
        self.wrappers: Dict[bytes, Tuple[Timeline, set]] = {}
        if forward:
            for wrapper_ph, share_asset in source.forwarders(asset_id).items():
                if wrapper_ph in blacklist:
                    continue
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


class Requirements:
    """Timelines for every gate and boost a campaign names, so a holder's
    eligibility and multiplier can be read at any height."""

    def __init__(self, campaign: Campaign, source: Source, before: int):
        self.params = campaign.params
        self.timelines: Dict[Tuple[int, bytes], Timeline] = {}
        requirements = [*self.params.gates, *(b.requirement for b in self.params.boosts)]
        # Address links group a holder's addresses for requirements, so a
        # campaign without any has no use for them (and skips verifying them).
        links = [l for l in source.links() if l.height < before] if requirements else []
        self.links = LinkBook(links, campaign.distributor_id)
        for req in requirements:
            key = (int(req.holding), req.asset)
            if key in self.timelines:
                continue
            if req.holding == Holding.NFT:
                events = source.nft_events(req.asset, before)
            else:
                events = source.holder_events(req.asset, before)
            self.timelines[key] = Timeline(events)

    def changes(self, start: int, end: int) -> List[int]:
        hs = set(self.links.changes(start, end))
        for tl in self.timelines.values():
            hs.update(tl.changes(start, end))
        return sorted(hs)

    def at(self, height: int) -> Dict[Tuple[int, bytes], Dict[bytes, int]]:
        return {key: tl.at(height) for key, tl in self.timelines.items()}

    def weights(self, campaign: Campaign, balances: Balances, height: int) -> Balances:
        """Eligible holders' weights at `height`: balance, filtered by the
        campaign's lists, minimum and gates, times the boost multiplier."""
        p = self.params
        held = self.at(height)

        def meets(req: Requirement, ph: bytes) -> bool:
            counts = held[(int(req.holding), req.asset)]
            return sum(counts.get(x, 0) for x in self.links.group(ph, height)) >= req.min_count

        out: Balances = {}
        for ph, b in balances.items():
            if not campaign.eligible(ph, b):
                continue
            if not all(meets(g, ph) for g in p.gates):
                continue
            if p.boosts:
                mult = p.multiplier_bps(tuple(meets(x.requirement, ph) for x in p.boosts))
                out[ph] = b * Fraction(mult, BPS)
            else:
                out[ph] = b
        return out


class Weights:
    """One campaign's eligible weights as a step function of height."""

    def __init__(self, campaign: Campaign, holdings: Holdings, reqs: Requirements):
        self.campaign, self.holdings, self.reqs = campaign, holdings, reqs
        self._at: Dict[int, Balances] = {}
        self._shares: Dict[int, Balances] = {}

    def at(self, height: int) -> Balances:
        if height not in self._at:
            self._at[height] = self.reqs.weights(self.campaign, self.holdings.at(height), height)
        return self._at[height]

    def segments(self, lo: int, hi: int) -> List[Tuple[int, int, Balances]]:
        """[lo, hi) cut wherever a balance, a gate, a boost or a link changes."""
        cuts = [lo, *sorted(set(self.holdings.changes(lo, hi)) | set(self.reqs.changes(lo, hi))), hi]
        return [(a, b, self.at(a)) for a, b in zip(cuts, cuts[1:])]

    def lookback_share(self, height: int) -> Balances:
        """Each holder's share of eligible weight, averaged block by block over
        the `fee_window` blocks ending at `height` (inclusive). Blocks with no
        eligible weight are left out of the average. A deposit held for one
        block adds at most one block's share, whatever its size."""
        if height not in self._shares:
            acc: Balances = defaultdict(Fraction)
            lo = max(0, height + 1 - self.campaign.params.fee_window)
            for a, b, bal in self.segments(lo, height + 1):
                total = sum(bal.values())
                if total:
                    for ph, x in bal.items():
                        acc[ph] += (b - a) * x / total
            blocks = sum(acc.values())
            self._shares[height] = {ph: v / blocks for ph, v in acc.items()} if blocks else {}
        return self._shares[height]


# --- one campaign over one window ---------------------------------------------


def fee_buckets(campaign: Campaign, lo: int, hi: int) -> List[Tuple[int, int]]:
    """The campaign's fee buckets that close in (lo, hi]. Bucket k is
    [start + k*W, min(start + (k+1)*W, end)) with W = params.fee_window."""
    w, out = campaign.params.fee_window, []
    k = max(0, (lo - campaign.start) // w)
    while True:
        bs = campaign.start + k * w
        be = min(bs + w, campaign.end)
        if bs >= campaign.end or be > hi:
            return out
        if be > lo:
            out.append((bs, be))
        k += 1


def _time_weight(campaign: Campaign, segments, bps: int, credit: Balances) -> None:
    """Split `bps` of each segment's emission by eligible weight (§6.2). A
    segment with no eligible holder stays unallocated, reclaimable."""
    for a, b, bal in segments:
        total = sum(bal.values())
        if not total:
            continue
        budget = Fraction((campaign.emittable(b) - campaign.emittable(a)) * bps, BPS)
        for ph, x in bal.items():
            credit[ph] += budget * x / total


def _fee_bucket(campaign: Campaign, weights: Weights, events, bs: int, be: int, credit: Balances) -> None:
    """Split one closed bucket's fee budget over its fee events (§6.3)."""
    p = campaign.params
    points = []
    for h, w in events:
        if w <= 0:
            continue
        share = weights.lookback_share(h)
        if share:
            points.append((w, share))
    if not points:
        # A bucket with no fee-paying action spends its fee share on time
        # weighting, so the budget is never stranded because a pool was quiet.
        _time_weight(campaign, weights.segments(bs, be), p.fees_bps, credit)
        return
    budget = Fraction((campaign.emittable(be) - campaign.emittable(bs)) * p.fees_bps, BPS)
    total = sum(w for w, _ in points)
    for w, share in points:
        for ph, x in share.items():
            credit[ph] += budget * w * x / total


def accrue(campaign: Campaign, source: Source, start: int, end: int) -> Balances:
    """Exact credit each holder earns from `campaign` in [start, end): the
    time share of every block in the window, plus the fee share of every fee
    bucket that closes in (start, end]."""
    window = campaign.active_in(start, end)
    if window is None:
        return {}
    lo, hi = window
    p = campaign.params
    asset = source.lp_asset(campaign.target) if campaign.kind == Kind.LP_HOLD else campaign.target
    holdings = Holdings(source, asset, hi, p.forward, p.blacklist)
    weights = Weights(campaign, holdings, Requirements(campaign, source, hi))

    credit: Balances = defaultdict(Fraction)
    _time_weight(campaign, weights.segments(lo, hi), p.time_bps, credit)
    buckets = fee_buckets(campaign, lo, hi) if p.fees_bps else []
    if buckets:
        events = [(h, fee_weight(w)) for h, w in source.fee_events(campaign.target, buckets[0][0], buckets[-1][1])]
        for bs, be in buckets:
            _fee_bucket(campaign, weights, [(h, w) for h, w in events if bs <= h < be], bs, be, credit)
    return dict(credit)


# --- one root for one distributor ----------------------------------------------


def committed_links(state: EngineState, links: Iterable[Link], end: int) -> List[Link]:
    """The link log prefix a root ending at `end` commits: entries with
    heights below `end`. Refuses a log that rewrites the prefix the last
    posted root committed, or appends an entry dated before that root's end."""
    log = check_log(links)
    if log_hash(log[: state.link_count]) != state.links_hash:
        raise ValueError(f"the link log rewrites the prefix root {state.root_id} committed")
    if state.height is not None:
        for link in log[state.link_count:]:
            if link.height < state.height:
                raise ValueError(f"link recorded at {link.height}, inside a window root {state.root_id} settled")
    return [link for link in log if link.height < end]


def run_epoch(
    state: EngineState,
    campaigns: Iterable[Campaign],
    source: Source,
    end: int,
) -> Tuple[EngineState, Root]:
    """Accrue [state.height, end) and build root `state.root_id + 1`.

    Returns a new state and the root; `state` is not modified. If nothing
    claimable changed in the window, the state comes back unchanged with a root whose `root` is None: nothing is to be
    posted, and the next run covers the longer window.
    """
    campaigns = [c for c in campaigns if c.distributor_id == state.distributor_id]
    if not campaigns:
        raise ValueError("no campaigns for this distributor")
    start = state.height if state.height is not None else min(c.start for c in campaigns)
    if end <= start:
        raise ValueError("window must end after it starts")
    links = committed_links(state, source.links(), end)

    accrued = {cid: dict(v) for cid, v in state.accrued.items()}
    changed = False
    for c in campaigns:
        acc = accrued.setdefault(c.id, {})
        for ph, x in accrue(c, source, start, end).items():
            if x:
                acc[ph] = acc.get(ph, Fraction(0)) + x
                changed = True
    if not changed:
        return state, Root(state.distributor_id, state.root_id + 1, start, end, None, {}, [])

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
        return state, Root(state.distributor_id, state.root_id + 1, start, end, None, {}, [])
    leaves = [claim_leaf(state.distributor_id, cid, ph, amount) for cid, ph, amount in rows]
    root, proofs = build_tree(leaves)
    claims = [Claim(cid, ph, amount, leaf, proofs[leaf]) for (cid, ph, amount), leaf in zip(rows, leaves)]

    root_id = state.root_id + 1
    links_hash = log_hash(links)
    new_state = EngineState(state.distributor_id, root_id, end, accrued, len(links), links_hash)
    return new_state, Root(state.distributor_id, root_id, start, end, root, dict(totals), claims, len(links), links_hash)
