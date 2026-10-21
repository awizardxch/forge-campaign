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
  campaigns one creator may hold. A distributor's table holds a fixed number
  of campaigns, and before these rules 32 one-mojo campaigns filled it for 32
  mojos (review 2026-10-19, FLPCR-04). The admin can now clear an abandoned
  campaign on chain once its window ends; these rules keep the table from
  filling in the first place.

The registry is read from chain by the caller; this module only applies it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable

from .model import BLOCKS_PER_DAY, Campaign, Kind

XCH = bytes(32)


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

    min_amount: Dict[bytes, int] = field(default_factory=dict)
    min_per_day: Dict[bytes, int] = field(default_factory=dict)
    max_live_per_creator: int = 4
    # NFT_HOLD targets the operator will sign for (minting DID launcher ids);
    # empty means any collection.
    nft_collections: FrozenSet[bytes] = frozenset()


def check_campaign(campaign: Campaign, registry: ForgeRegistry, rules: RouterRules = RouterRules(),
                   live_for_creator: int = 0) -> None:
    """`live_for_creator`: how many campaigns this creator already has live
    in the distributor (the caller reads it from chain state)."""
    asset = campaign.reward_asset
    if campaign.amount < rules.min_amount.get(asset, 0):
        raise NotRoutable("amount is below the minimum for this reward asset")
    if campaign.amount * BLOCKS_PER_DAY < rules.min_per_day.get(asset, 0) * campaign.length:
        raise NotRoutable("rate is below the minimum per day for this reward asset")
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
