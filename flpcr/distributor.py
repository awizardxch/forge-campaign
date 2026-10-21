"""Reference model of the distributor singleton, and its one Config.

The Rue puzzles refuse exactly what this refuses and pay exactly what this
pays; `docs/PUZZLES.md` is the leaf-by-leaf text and this is the same rules as
running code. `tests/test_differential.py` holds the two to it. Every method
that refuses raises `Refused` and leaves the state untouched.

Claims follow Merkl: the active root lists each holder's cumulative earnings
per campaign; `claim` pays `cumulative - claimed` and records the new claimed
total (on chain: one slot per (campaign, holder) in a sorted slot list, the
Forge registry's pattern). Each campaign carries its own claim window, set by
its creator. Once it has passed the creator may reclaim everything holders did
not claim, earned or not. Until the creator actually does, holders can still
claim: the window only says when reclaiming opens, not when claiming closes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from .merkle import Proof, claim_leaf, root_from_proof
from .model import BLOCKS_PER_DAY, DEFAULT_FEE_BPS, Campaign

# shared.rue HEIGHT_WINDOW: a leaf's `h` may trail the block that includes it
# by up to HEIGHT_WINDOW - 1 blocks, so every delay counted from `h` adds it.
HEIGHT_WINDOW = 32
# Mojos every delegate grant or revoke must leave as network fee (FLPCR-12).
DEFAULT_DELEGATE_FEE = 50_000_000
# Mojos of XCH a disputer locks to contest a pending root; refunded if upheld,
# forfeited to the dev multisig if not. What Merkl does with its dispute token.
DEFAULT_DISPUTE_BOND = 5_000_000_000_000


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Config:
    """Everything curried into a distributor's leaves (shared.rue Config,
    minus the two slot curry hashes the driver derives). Fixed for the
    distributor's life."""

    distributor_id: bytes
    reward_asset: bytes
    dev_fee_ph: bytes  # receives the creation fee in the create spend itself
    # The dev multisig: may reclaim a campaign for its creator (paid to the
    # creator) and cancel a pending operator rotation. Usually dev_fee_ph.
    admin_ph: bytes
    guardian_ph: bytes
    # Only campaigns this router authorizes can be created here. Each operator
    # (us now, others later) runs its own distributor, router key, engine and
    # front end; a campaign never sits in a distributor nobody scores.
    router_ph: bytes
    fee_bps: int = DEFAULT_FEE_BPS
    # Production defaults. A root becomes claimable at least dispute_blocks
    # after it lands, so this is also how fresh mid-campaign claims can be.
    dispute_blocks: int = 38  # ~12 min
    # How long a rotated operator key waits before it takes effect; longer
    # than a dispute window so a takeover is public first.
    rotate_delay: int = BLOCKS_PER_DAY
    delegate_fee: int = DEFAULT_DELEGATE_FEE
    # Bounds on campaign length (end - start). Within a campaign, holders
    # claim at whatever interval they like; each claim costs them a coin
    # spend, so how often is their call.
    min_campaign_length: int = 77  # ~24 min
    max_campaign_length: int = 365 * BLOCKS_PER_DAY
    max_campaigns: int = 32
    dispute_bond: int = DEFAULT_DISPUTE_BOND

    def __post_init__(self) -> None:
        for name in ("distributor_id", "reward_asset", "dev_fee_ph", "admin_ph", "guardian_ph", "router_ph"):
            if len(getattr(self, name)) != 32:
                raise ValueError(f"{name} must be 32 bytes")
        if self.dispute_blocks <= 0:
            raise ValueError("dispute_blocks must be positive")
        if self.rotate_delay <= self.dispute_blocks:
            raise ValueError("rotate_delay must exceed dispute_blocks")  # init.rue asserts the same
        if self.delegate_fee < 0:
            raise ValueError("delegate_fee cannot be negative")
        if self.dispute_bond <= 0:
            raise ValueError("dispute_bond must be positive")  # init.rue asserts the same
        if not 0 < self.min_campaign_length <= self.max_campaign_length:
            raise ValueError("bad campaign length bounds")
        if self.max_campaigns <= 0:
            raise ValueError("max_campaigns must be positive")

    @property
    def min_claim_window(self) -> int:
        """The creator's claim window has only a technical floor: room for the
        previous root's dispute window, one veto and repost of the root
        covering `end`, and the HEIGHT_WINDOW slack, before reclaiming opens
        (FLPCR-08). Its ceiling is the longest campaign."""
        return 3 * self.dispute_blocks + HEIGHT_WINDOW

    @property
    def min_start_lead(self) -> int:
        """A campaign starts at least this many blocks after the `h` its create
        names (FLPCR-15); quote `start >= tip + min_start_lead`."""
        return HEIGHT_WINDOW

    @classmethod
    def testnet(cls, *args, **kwargs) -> "Config":
        """Short clocks for testing: ~2.5 min disputes, campaigns from ~5 min."""
        kwargs.setdefault("dispute_blocks", 8)
        kwargs.setdefault("rotate_delay", 24)
        kwargs.setdefault("delegate_fee", 1_000_000)
        kwargs.setdefault("dispute_bond", 100_000_000_000)
        kwargs.setdefault("min_campaign_length", 16)
        return cls(*args, **kwargs)


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
class Rotation:
    engine_ph: bytes
    effective_at: int


@dataclass
class Dispute:
    root_id: int
    disputer_ph: bytes
    bond_parent: bytes


@dataclass
class Distributor:
    config: Config
    engine_ph: bytes
    campaigns: Dict[bytes, CampaignRecord] = field(default_factory=dict)
    active: Optional[PostedRoot] = None
    pending: Optional[PostedRoot] = None
    rotation: Optional[Rotation] = None
    dispute: Optional[Dispute] = None
    bonds_paid: Dict[bytes, int] = field(default_factory=dict)  # where released bonds went
    claimed: Dict[Tuple[bytes, bytes], int] = field(default_factory=dict)  # the claim slots
    # (owner, delegate) -> active: the delegation slots. A key exists once.
    delegations: Dict[Tuple[bytes, bytes], bool] = field(default_factory=dict)
    dev_fee_paid: int = 0  # total sent to dev_fee_ph; never held by the distributor
    reserve: int = 0  # mojos in the reserve coin

    # -- helpers ---------------------------------------------------------------

    def _settle(self, h: int) -> None:
        """What the prologue of whichever spend comes next does: a pending root
        whose dispute window has passed becomes the active one, and a rotation
        whose delay has passed becomes the operator key."""
        if self.pending is not None and h >= self.pending.activates_at and self.dispute is None:
            self.active, self.pending = self.pending, None
        if self.rotation is not None and h >= self.rotation.effective_at:
            self.engine_ph, self.rotation = self.rotation.engine_ph, None

    def _last_posted(self) -> Optional[PostedRoot]:
        return self.pending or self.active

    def _delegated(self, owner: bytes, delegate: bytes) -> bool:
        return self.delegations.get((owner, delegate), False)

    def check_invariant(self) -> None:
        owed = sum(r.campaign.budget - r.claimed for r in self.campaigns.values())
        if self.reserve != owed:
            raise AssertionError(f"reserve {self.reserve} != owed {owed}")

    # -- leaves ------------------------------------------------------------------

    def create(self, h: int, sender: bytes, campaign: Campaign, deposit: int, consented_by: bytes) -> bytes:
        """`sender` is the puzzle hash of the coin sending the router's
        message; `consented_by` the puzzle hash of the coin whose spend sends
        the creator's consent. On chain both messages carry the campaign id."""
        self._settle(h)
        c = self.config
        if sender != c.router_ph:
            raise Refused("only the distributor's router may create campaigns")
        if len(campaign.creator) != 32:
            raise Refused("creator must be a 32-byte puzzle hash")
        if consented_by != campaign.creator:
            raise Refused("the creator must consent by spending a coin of their own")
        if campaign.distributor_id != c.distributor_id or campaign.reward_asset != c.reward_asset:
            raise Refused("campaign is for another distributor")
        if campaign.fee_bps != c.fee_bps:
            raise Refused("fee does not match the distributor's")
        if campaign.start < h + c.min_start_lead:
            raise Refused("campaign would start too soon")
        if campaign.start > h + c.max_campaign_length:
            raise Refused("campaign starts too far ahead")
        if not c.min_campaign_length <= campaign.length <= c.max_campaign_length:
            raise Refused("campaign length out of bounds")
        if not c.min_claim_window <= campaign.claim_window <= c.max_campaign_length:
            raise Refused("claim window out of bounds")
        if campaign.id in self.campaigns:
            raise Refused("campaign id already exists")
        if len(self.campaigns) >= c.max_campaigns:
            raise Refused("distributor is full")
        # `deposit` is what the bundle supplies. On chain the reserve is
        # recreated at `reserve + budget` and pays the fee to the dev address,
        # so the reserve takes in `amount` and the bundle must supply at least
        # that (conservation is checked over the whole bundle). For an XCH
        # reward anything over it is ordinary slack, i.e. network fee; a CAT
        # ring cannot carry excess, so a CAT create returns change instead.
        if deposit < campaign.amount:
            raise Refused("deposit is short")
        self.campaigns[campaign.id] = CampaignRecord(campaign)
        # The dev fee leaves in this spend; only the budget stays in the reserve.
        self.dev_fee_paid += campaign.fee
        self.reserve += campaign.budget
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
        # Counted from h + HEIGHT_WINDOW: never fewer than dispute_blocks after
        # the block that includes the post, however far `h` trails it.
        activates_at = h + HEIGHT_WINDOW + self.config.dispute_blocks
        self.pending = PostedRoot(root_id, start, end, root, dict(totals), activates_at)

    def veto(self, h: int, sender: bytes, root_id: int) -> None:
        self._settle(h)
        if sender != self.config.guardian_ph:
            raise Refused("only the guardian may veto")
        if self.pending is None or self.pending.root_id != root_id:
            raise Refused("no such root in its dispute window")
        if self.dispute is not None:
            raise Refused("a disputed root is settled by resolve")
        self.pending = None

    def dispute_root(self, h: int, root_id: int, disputer_ph: bytes, bond: int, bond_parent: bytes) -> None:
        """Anyone locks `bond` (exactly the configured amount, in a bond coin
        whose parent is `bond_parent`) to contest the pending root; it then
        cannot activate until resolved."""
        self._settle(h)
        if self.pending is None or self.pending.root_id != root_id:
            raise Refused("no such root in its dispute window")
        if self.dispute is not None:
            raise Refused("a dispute is already open")
        if len(disputer_ph) != 32:
            raise Refused("disputer must be a 32-byte puzzle hash")
        if bond != self.config.dispute_bond:
            raise Refused("the bond must be exactly dispute_bond")
        self.dispute = Dispute(root_id, disputer_ph, bond_parent)

    def resolve(self, h: int, sender: bytes, upheld: bool) -> Tuple[bytes, int]:
        """The guardian or admin settles the open dispute. Upheld: the root is
        revoked and the bond refunded to the disputer. Rejected: the bond goes
        to the dev multisig and the dispute window restarts. Returns where the
        bond went."""
        self._settle(h)
        if self.dispute is None or self.pending is None:
            raise Refused("no dispute open")
        if sender not in (self.config.guardian_ph, self.config.admin_ph):
            raise Refused("only the guardian or the admin may resolve a dispute")
        recipient = self.dispute.disputer_ph if upheld else self.config.dev_fee_ph
        if upheld:
            self.pending = None
        else:
            self.pending.activates_at = h + HEIGHT_WINDOW + self.config.dispute_blocks
        self.dispute = None
        self.bonds_paid[recipient] = self.bonds_paid.get(recipient, 0) + self.config.dispute_bond
        return recipient, self.config.dispute_bond

    def rotate(self, h: int, sender: bytes, new_engine_ph: bytes) -> None:
        """The guardian proposes a new operator key. It takes effect
        rotate_delay blocks later (plus the HEIGHT_WINDOW slack) unless the
        admin cancels it; a newer proposal replaces it and restarts the clock."""
        self._settle(h)
        if sender != self.config.guardian_ph:
            raise Refused("only the guardian may rotate the engine")
        if len(new_engine_ph) != 32:
            raise Refused("operator key must be a 32-byte puzzle hash")
        self.rotation = Rotation(new_engine_ph, h + HEIGHT_WINDOW + self.config.rotate_delay)

    def cancel_rotation(self, h: int, sender: bytes, engine_ph: bytes) -> None:
        self._settle(h)
        if sender != self.config.admin_ph:
            raise Refused("only the admin may cancel a rotation")
        if self.rotation is None or self.rotation.engine_ph != engine_ph:
            raise Refused("no such rotation pending")
        self.rotation = None

    def claim(self, h: int, campaign_id: bytes, puzzle_hash: bytes, cumulative: int,
              proof: Proof, authorized_by: bytes, by_spend: bool = True) -> Tuple[bytes, int]:
        """Only the holder, or a delegate the holder granted, can claim, and
        only by spending a coin at its own address (`by_spend`): a bare
        signature is refused. `authorized_by` is that address. The payment
        always goes to `puzzle_hash`."""
        self._settle(h)
        if len(puzzle_hash) != 32:
            raise Refused("holder must be a 32-byte puzzle hash")
        if not by_spend:
            raise Refused("a claim costs the claimer a coin spend")
        if authorized_by != puzzle_hash and not self._delegated(puzzle_hash, authorized_by):
            raise Refused("only the holder or their delegate can claim")
        rec = self.campaigns.get(campaign_id)
        if rec is None:
            raise Refused("campaign has been reclaimed")
        # No deadline here: past the claim window a holder can still claim
        # until the creator's reclaim lands. Whichever spend comes first wins.
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

    def set_delegate(self, h: int, owner: bytes, delegate: bytes, grant: bool, authorized_by: bytes) -> None:
        """The owner grants or revokes a delegate that may act for them. One
        record per (owner, delegate): granting twice and revoking once
        revokes. A revoke of a pair never granted has nothing to change."""
        self._settle(h)
        if authorized_by != owner:
            raise Refused("only the owner can grant or revoke a delegate")
        if len(owner) != 32 or len(delegate) != 32:
            raise Refused("owner and delegate must be 32-byte puzzle hashes")
        if not grant and (owner, delegate) not in self.delegations:
            raise Refused("no such delegate")
        self.delegations[(owner, delegate)] = bool(grant)

    def reclaim(self, h: int, campaign_id: bytes, authorized_by: bytes, as_admin: bool = False) -> Tuple[bytes, int]:
        """Once the campaign's claim window has passed, the creator gets back
        whatever holders left: budget never earned plus earnings never claimed.
        The creator, a delegate the creator granted, or the admin
        (`as_admin`, authorized by admin_ph) can reclaim; the payment always
        goes to the creator."""
        self._settle(h)
        rec = self.campaigns.get(campaign_id)
        if rec is None:
            raise Refused("no such campaign")
        creator = rec.campaign.creator
        if as_admin:
            if authorized_by != self.config.admin_ph:
                raise Refused("only the admin may reclaim as admin")
        elif authorized_by != creator and not self._delegated(creator, authorized_by):
            raise Refused("only the creator, their delegate or the admin can reclaim")
        if h < rec.campaign.reclaim_from:
            raise Refused("claim window still open")
        amount = rec.campaign.budget - rec.claimed
        del self.campaigns[campaign_id]
        self.reserve -= amount
        return creator, amount
