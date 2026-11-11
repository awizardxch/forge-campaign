"""What our router checks before it authorizes a campaign.

The distributor puzzle accepts any campaign its router signs for; these are
the router's own rules, applied at quote time:

* the reward asset, and a CAT_HOLD target, must trade in a pool listed in the
  Forge registry (XCH always qualifies). A registered pool gives every such
  asset an on-chain price, the pool's oracle TWAP, so APR, TVL and the
  create preview are priced from chain rather than from a list we maintain;
* an LP_HOLD target must itself be a registered Forge pool;
* the campaign must clear the operator's `RouterRules`: a minimum amount and a
  minimum per-day rate in the reward asset, and a cap on how many live
  campaigns one creator may hold, and a fee window of at least
  `min_fee_window` blocks (a day by default; `Params` itself refuses anything
  under an hour on every network). A distributor's table holds a fixed number
  of campaigns, and before these rules 32 one-mojo campaigns filled it for 32
  mojos (review 2026-10-19, FLPCR-04). The admin can now clear an abandoned
  campaign on chain once its window ends; these rules keep the table from
  filling in the first place.

* by default (audit 2026-11-10, CS-04 and CS-05) the router also signs only for LP_HOLD
  campaigns, of at least `DEFAULT_MIN_XCH` when the reward is XCH, that start within
  `max_start_lead` blocks and leave holders a claim window of at least
  `min_claim_window` blocks. The puzzle's own floor for the claim window is about an
  hour and a half; a creator who picked it and kept the service from posting for that
  long took back what holders had earned. An operator widens any of these on purpose,
  in FLPCR_ROUTER_RULES; `RouterRules.permissive()` is for tests and probes.

The registry is read from chain by the caller; this module only applies it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, Optional

from .model import BLOCKS_PER_DAY, MIN_FEE_WINDOW, Campaign, Kind

XCH = bytes(32)
DEFAULT_MIN_XCH = 100_000_000_000  # 0.1 XCH: the smallest XCH campaign the router signs for by default


class NotRoutable(Exception):
    pass


@dataclass(frozen=True)
class ForgeRegistry:
    # pool launcher id -> the asset ids it holds (ZERO_32 for XCH)
    pools: Dict[bytes, FrozenSet[bytes]]

    @classmethod
    def from_pools(cls, pools: Dict[bytes, Iterable[bytes]]) -> "ForgeRegistry":
        return cls({k: frozenset(v) for k, v in pools.items()})

    @property
    def assets(self) -> FrozenSet[bytes]:
        out = {XCH}
        for held in self.pools.values():
            out |= held
        return frozenset(out)

    def is_priced(self, asset_id: bytes) -> bool:
        return asset_id in self.assets


@dataclass(frozen=True)
class RouterRules:
    """The operator's floor for what it will sign, per reward asset (mojos of
    that asset; an asset with no entry has no floor). The values are the
    operator's to set; nothing here is a protocol constant."""

    min_amount: Dict[bytes, int] = field(default_factory=lambda: {XCH: DEFAULT_MIN_XCH})
    min_per_day: Dict[bytes, int] = field(default_factory=dict)
    max_live_per_creator: int = 4
    # The kinds the router signs for. LP_HOLD only until the live sources score the others
    # (CAT_HOLD credits nobody today; NFT_HOLD reads metadata from URLs its minter chose).
    kinds: FrozenSet[Kind] = frozenset({Kind.LP_HOLD})
    # The shortest claim window the router signs for: holders need days, not the puzzle's
    # floor of four root cycles, to claim before the creator may reclaim.
    min_claim_window: int = 7 * BLOCKS_PER_DAY
    # The furthest ahead a campaign may start: an idle row holds one of the table's slots.
    max_start_lead: int = 30 * BLOCKS_PER_DAY
    # NFT_HOLD targets the operator will sign for (minting DID launcher ids);
    # empty means any collection.
    nft_collections: FrozenSet[bytes] = frozenset()
    # The shortest `params.fee_window` the router signs for. The id commits to
    # it, so it is checked at quote time. Production: one day (the default);
    # a testnet router may lower it as far as MIN_FEE_WINDOW.
    min_fee_window: int = BLOCKS_PER_DAY

    def __post_init__(self) -> None:
        if self.min_fee_window < MIN_FEE_WINDOW:
            raise ValueError(f"min_fee_window cannot be under the protocol floor of {MIN_FEE_WINDOW} blocks")

    @classmethod
    def permissive(cls, **kw) -> "RouterRules":
        """No amount floor, every kind, the puzzle's own claim window and start bounds: for
        tests and probes. Never a service's rules."""
        base = dict(min_amount={}, kinds=frozenset(Kind), min_claim_window=0, max_start_lead=10**12)
        base.update(kw)
        return cls(**base)


def check_campaign(campaign: Campaign, registry: ForgeRegistry, rules: RouterRules = RouterRules(),
                   live_for_creator: int = 0, height: Optional[int] = None) -> None:
    """`live_for_creator`: how many campaigns this creator already has live
    in the distributor (the caller reads it from chain state). `height`: the chain's
    height now, for the start bound (skipped when the caller has none)."""
    asset = campaign.reward_asset
    if campaign.kind not in rules.kinds:
        raise NotRoutable(f"this router does not sign for {campaign.kind.name} campaigns")
    if campaign.claim_window < rules.min_claim_window:
        raise NotRoutable(f"claim window is under the {rules.min_claim_window} blocks this router signs for")
    if height is not None and campaign.start > height + rules.max_start_lead:
        raise NotRoutable(f"start is more than {rules.max_start_lead} blocks ahead")
    if campaign.amount < rules.min_amount.get(asset, 0):
        raise NotRoutable("amount is below the minimum for this reward asset")
    if campaign.amount * BLOCKS_PER_DAY < rules.min_per_day.get(asset, 0) * campaign.length:
        raise NotRoutable("rate is below the minimum per day for this reward asset")
    if campaign.params.fee_window < rules.min_fee_window:
        raise NotRoutable(f"fee window is under the {rules.min_fee_window} blocks this router signs for")
    if live_for_creator >= rules.max_live_per_creator:
        raise NotRoutable("this creator already has the most live campaigns allowed")
    if not registry.is_priced(campaign.reward_asset):
        raise NotRoutable("reward asset is not in any Forge registry pool")
    if campaign.kind == Kind.LP_HOLD and campaign.target not in registry.pools:
        raise NotRoutable("target is not a registered Forge pool")
    if campaign.kind == Kind.CAT_HOLD and not registry.is_priced(campaign.target):
        raise NotRoutable("CAT is not in any Forge registry pool")
    if campaign.kind == Kind.NFT_HOLD and rules.nft_collections and campaign.target not in rules.nft_collections:
        raise NotRoutable("collection is not one this router signs for")
