"""Reference model of the distributor singleton.

The Rue puzzles in phase 2 must refuse exactly what this refuses and pay exactly
what this pays; `docs/PUZZLES.md` is the leaf-by-leaf text and this is the same
rules as running code, so the engine's output can be pushed through a claim
lifecycle in tests long before a puzzle exists. Every method that refuses
raises `Refused` and leaves the state untouched.

Claims follow Merkl: the active root lists each holder's cumulative earnings
per campaign; `claim` pays `cumulative - claimed` and records the new claimed
total (on chain: one slot per (campaign, holder) in a sorted slot list, the
Forge registry's pattern). Once a campaign's claim window has closed, its
creator reclaims everything holders did not claim, earned or not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .merkle import Proof, claim_leaf, root_from_proof
from .model import DEFAULT_FEE_BPS, Campaign

BLOCKS_PER_DAY = 4608  # 18.75 s per block


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Config:
    distributor_id: bytes
    reward_asset: bytes
    treasury_ph: bytes
    guardian_ph: bytes
    fee_bps: int = DEFAULT_FEE_BPS
    dispute_blocks: int = 1152  # ~6 h
    claim_window: int = 30 * BLOCKS_PER_DAY  # after a campaign's end, before the creator may reclaim
    max_campaigns: int = 32


@dataclass
class CampaignRecord:
    campaign: Campaign
    claimed: int = 0  # sum of every holder's claims


@dataclass
class PostedRoot:
    root_id: int
    start: int
    end: int
    root: bytes
    totals: Dict[bytes, int]  # campaign id -> sum of its cumulatives in this root
    activates_at: int


@dataclass
class Distributor:
    config: Config
    engine_ph: bytes
    campaigns: Dict[bytes, CampaignRecord] = field(default_factory=dict)
    active: Optional[PostedRoot] = None
    pending: Optional[PostedRoot] = None
    claimed: Dict[Tuple[bytes, bytes], int] = field(default_factory=dict)  # the claim slots
    fees_owed: int = 0
    reserve: int = 0  # mojos in the reserve coin

    # -- helpers ---------------------------------------------------------------

    def _settle(self, h: int) -> None:
        """A pending root whose dispute window has passed becomes the active one.
        On chain this happens in the prologue of whichever spend comes next."""
        if self.pending is not None and h >= self.pending.activates_at:
            self.active, self.pending = self.pending, None

    def _last_posted(self) -> Optional[PostedRoot]:
        return self.pending or self.active

    def check_invariant(self) -> None:
        owed = sum(r.campaign.budget - r.claimed for r in self.campaigns.values())
        if self.reserve != owed + self.fees_owed:
            raise AssertionError(f"reserve {self.reserve} != owed {owed} + fees {self.fees_owed}")

    # -- leaves ------------------------------------------------------------------

    def create(self, h: int, campaign: Campaign, deposit: int) -> bytes:
        self._settle(h)
        c = self.config
        if campaign.distributor_id != c.distributor_id or campaign.reward_asset != c.reward_asset:
            raise Refused("campaign is for another distributor")
        if campaign.fee_bps != c.fee_bps:
            raise Refused("fee does not match the distributor's")
        if campaign.start < h:
            raise Refused("campaign would start in the past")
        if campaign.id in self.campaigns:
            raise Refused("campaign id already exists")
        if len(self.campaigns) >= c.max_campaigns:
            raise Refused("distributor is full")
        if deposit < campaign.amount:
            raise Refused("deposit is short")
        # Anything over the stated amount is slack in the bundle: it is kept as
        # fee so the reserve and the state agree (V14's settlement lesson).
        self.campaigns[campaign.id] = CampaignRecord(campaign)
        self.fees_owed += campaign.fee + (deposit - campaign.amount)
        self.reserve += deposit
        return campaign.id

    def post_root(self, h: int, sender: bytes, root_id: int, start: int, end: int,
                  root: bytes, totals: Dict[bytes, int]) -> None:
        self._settle(h)
        if sender != self.engine_ph:
            raise Refused("only the engine may post")
        if self.pending is not None:
            raise Refused("a root is still in its dispute window")
        last = self._last_posted()
        if root_id != (last.root_id + 1 if last else 1):
            raise Refused("root id out of sequence")
        if last is not None and start != last.end:
            raise Refused("roots must tile the timeline")
        if not start < end <= h:
            raise Refused("bad window")
        prev = last.totals if last else {}
        for cid in prev:
            if cid in self.campaigns and cid not in totals:
                raise Refused("a campaign total cannot disappear")
        for cid, total in totals.items():
            rec = self.campaigns.get(cid)
            if rec is None:
                raise Refused("unknown campaign")
            if total < prev.get(cid, 0):
                raise Refused("cumulative totals never decrease")
            if total > rec.campaign.emittable(end):
                raise Refused("over the campaign's emission schedule")
        self.pending = PostedRoot(root_id, start, end, root, dict(totals), h + self.config.dispute_blocks)

    def veto(self, h: int, sender: bytes, root_id: int) -> None:
        self._settle(h)
        if sender != self.config.guardian_ph:
            raise Refused("only the guardian may veto")
        if self.pending is None or self.pending.root_id != root_id:
            raise Refused("no such root in its dispute window")
        self.pending = None

    def rotate(self, h: int, sender: bytes, new_engine_ph: bytes) -> None:
        self._settle(h)
        if sender != self.config.guardian_ph:
            raise Refused("only the guardian may rotate the engine")
        self.engine_ph = new_engine_ph

    def claim(self, h: int, campaign_id: bytes, puzzle_hash: bytes, cumulative: int,
              proof: Proof) -> Tuple[bytes, int]:
        """Anyone may submit; the payment always goes to `puzzle_hash`."""
        self._settle(h)
        rec = self.campaigns.get(campaign_id)
        if rec is None:
            raise Refused("campaign is closed")
        if h >= rec.campaign.end + self.config.claim_window:
            raise Refused("claim window has closed")
        if self.active is None:
            raise Refused("no active root")
        leaf = claim_leaf(self.config.distributor_id, campaign_id, puzzle_hash, cumulative)
        if root_from_proof(leaf, proof) != self.active.root:
            raise Refused("proof does not match the active root")
        key = (campaign_id, puzzle_hash)
        amount = cumulative - self.claimed.get(key, 0)
        if amount <= 0:
            raise Refused("nothing to claim")
        if rec.claimed + amount > self.active.totals.get(campaign_id, 0):
            raise Refused("claims would exceed the campaign's posted total")
        self.claimed[key] = cumulative
        rec.claimed += amount
        self.reserve -= amount
        return puzzle_hash, amount

    def reclaim(self, h: int, campaign_id: bytes) -> Tuple[bytes, int]:
        """After the claim window, the creator gets back whatever holders left:
        budget never earned plus earnings never claimed. Anyone may submit."""
        self._settle(h)
        rec = self.campaigns.get(campaign_id)
        if rec is None:
            raise Refused("no such campaign")
        if h < rec.campaign.end + self.config.claim_window:
            raise Refused("claim window still open")
        amount = rec.campaign.budget - rec.claimed
        del self.campaigns[campaign_id]
        self.reserve -= amount
        return rec.campaign.creator, amount

    def collect(self, h: int) -> Tuple[bytes, int]:
        self._settle(h)
        if self.fees_owed <= 0:
            raise Refused("no fees owed")
        amount, self.fees_owed = self.fees_owed, 0
        self.reserve -= amount
        return self.config.treasury_ph, amount

    def claimable(self, h: int, campaign_id: bytes, puzzle_hash: bytes, cumulative: int) -> int:
        """What a claim would pay now, without submitting it (for the API)."""
        self._settle(h)
        if campaign_id not in self.campaigns:
            return 0
        return max(0, cumulative - self.claimed.get((campaign_id, puzzle_hash), 0))


def claims_for(root, puzzle_hash: bytes) -> List:
    """Every claim in an engine root that belongs to one holder."""
    return [c for c in root.claims if c.puzzle_hash == puzzle_hash]
