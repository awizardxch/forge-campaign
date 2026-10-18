"""What our router checks before it authorizes a campaign.

The distributor puzzle accepts any campaign its router signs for; these are
the router's own rules, applied at quote time:

* the reward asset, and a CAT_HOLD target, must trade in a pool listed in the
  Forge registry (XCH always qualifies). A registered pool gives every such
  asset an on-chain price, the pool's oracle TWAP, so APR, TVL and the
  create preview are priced from chain rather than from a list we maintain;
* an LP_HOLD target must itself be a registered Forge pool.

The registry is read from chain by the caller; this module only applies it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable

from .model import Campaign, Kind

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


def check_campaign(campaign: Campaign, registry: ForgeRegistry) -> None:
    if not registry.is_priced(campaign.reward_asset):
        raise NotRoutable("reward asset is not in any Forge registry pool")
    if campaign.kind == Kind.LP_HOLD and campaign.target not in registry.pools:
        raise NotRoutable("target is not a registered Forge pool")
    if campaign.kind == Kind.CAT_HOLD and not registry.is_priced(campaign.target):
        raise NotRoutable("CAT is not in any Forge registry pool")
