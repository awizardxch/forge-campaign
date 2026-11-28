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

* a CAT reward (owner, 2026-11-11) also needs the Forge router's word on that CAT
  (`CatPolicy`, read from `<forge>/api/campaign-assets`): the operator may turn a CAT off
  for campaigns or set its minimum; otherwise its floor is the equivalent of
  `CAT_FLOOR_USD` at the CAT's Forge price (the Markets page's chained price), and a CAT
  with no Forge price and no operator minimum is refused. The floor is the router's, not
  the puzzle's.

The registry is read from chain by the caller; this module only applies it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Dict, FrozenSet, Iterable, Optional

from .model import BLOCKS_PER_DAY, MIN_FEE_WINDOW, Campaign, Kind

XCH = bytes(32)
DEFAULT_MIN_XCH = 100_000_000_000  # 0.1 XCH: the smallest XCH campaign the router signs for by default
# What one root post costs the operator (flpcr.service.core MIN_FEE, the floor it pays). Each create
# prepays its campaign's posts to the operator's address (owner, 2026-11-25: "every deployment
# should fund its own deployment in lp campaigns just like the lp pairs deployment").
DEFAULT_POST_FEE = 2_000_000_000
# Posts a campaign is charged beyond one per `every` blocks of its length: the final root, and one
# repost after a veto or an upheld dispute.
EXTRA_POSTS = 2
CAT_FLOOR_USD = Decimal(1)  # the smallest CAT campaign, in dollars at the CAT's Forge price (owner, 2026-11-11)
CAT_DECIMALS = 3  # 1000 mojos to a CAT token


def usd_floor_mojos(price_usd, decimals: int = CAT_DECIMALS, floor_usd: Decimal = CAT_FLOOR_USD) -> Optional[int]:
    """Mojos of a CAT worth `floor_usd` at `price_usd` a token, rounded up; None without a usable price."""
    if price_usd is None or isinstance(price_usd, bool):
        return None
    try:
        price = Decimal(str(price_usd))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not price.is_finite() or price <= 0:
        return None
    return int((floor_usd * (Decimal(10) ** decimals) / price).to_integral_value(ROUND_CEILING))


@dataclass(frozen=True)
class CatPolicy:
    """The Forge router's word on one CAT as a campaign reward (GET <forge>/api/campaign-assets).

    `enabled`: the operator has not turned it off. `min_amount`: the operator's minimum in mojos,
    or None for the dollar floor at `price_usd` (USD per token)."""

    enabled: bool = True
    min_amount: Optional[int] = None
    price_usd: Optional[float] = None
    decimals: int = CAT_DECIMALS

    @property
    def floor(self) -> Optional[int]:
        """The smallest amount the router signs for; None when none can be set (no price, no minimum)."""
        if self.min_amount is not None:
            return self.min_amount
        return usd_floor_mojos(self.price_usd, self.decimals)

    @property
    def source(self) -> str:
        return "operator" if self.min_amount is not None else "usd"


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
    # The claim window the router signs for (owner, 2026-11-27: "unclaimed rewards after a
    # campaign ends should have about 48hrs then the deployer can claim it back ... leave no
    # longer than 30 days"). A campaign holds one of its distributor's slots until it is
    # reclaimed, so a short window also frees slots.
    min_claim_window: int = 2 * BLOCKS_PER_DAY
    max_claim_window: int = 30 * BLOCKS_PER_DAY
    # The furthest ahead a campaign may start: an idle row holds one of the table's slots.
    max_start_lead: int = 30 * BLOCKS_PER_DAY
    # NFT_HOLD targets the operator will sign for (minting DID launcher ids);
    # empty means any collection.
    nft_collections: FrozenSet[bytes] = frozenset()
    # The shortest `params.fee_window` the router signs for. The id commits to
    # it, so it is checked at quote time. Production: one day (the default);
    # a testnet router may lower it as far as MIN_FEE_WINDOW.
    min_fee_window: int = BLOCKS_PER_DAY
    # A CAT reward needs the Forge router's CatPolicy for it (the $1 floor, the operator's
    # switch). Off only in permissive() rules, for tests and probes.
    cat_policy_required: bool = True
    # Mojos of XCH per root post the creator prepays to the operator's address (0: none).
    post_fee: int = DEFAULT_POST_FEE

    def __post_init__(self) -> None:
        if self.min_fee_window < MIN_FEE_WINDOW:
            raise ValueError(f"min_fee_window cannot be under the protocol floor of {MIN_FEE_WINDOW} blocks")

    @classmethod
    def permissive(cls, **kw) -> "RouterRules":
        """No amount floor, every kind, the puzzle's own claim window and start bounds: for
        tests and probes. Never a service's rules."""
        base = dict(min_amount={}, kinds=frozenset(Kind), min_claim_window=0, max_claim_window=10**12, max_start_lead=10**12,
                    cat_policy_required=False, post_fee=0)
        base.update(kw)
        return cls(**base)


def check_campaign(campaign: Campaign, registry: ForgeRegistry, rules: RouterRules = RouterRules(),
                   live_for_creator: int = 0, height: Optional[int] = None,
                   cat_policy: Optional[CatPolicy] = None) -> None:
    """`live_for_creator`: how many campaigns this creator already has live
    in the distributor (the caller reads it from chain state). `height`: the chain's
    height now, for the start bound (skipped when the caller has none). `cat_policy`:
    the Forge router's word on a CAT reward (ignored for XCH)."""
    asset = campaign.reward_asset
    if asset != XCH and (cat_policy is not None or rules.cat_policy_required):
        if cat_policy is None:
            raise NotRoutable("the Forge router's terms for this CAT could not be read")
        if not cat_policy.enabled:
            raise NotRoutable("the operator has turned this CAT off for campaigns")
        floor = cat_policy.floor
        if floor is None:
            raise NotRoutable("this CAT has no Forge price, so the router cannot set its $1 floor")
        if campaign.amount < floor:
            why = "the operator's minimum" if cat_policy.source == "operator" else "the equivalent of $1"
            raise NotRoutable(f"amount is below this CAT's floor of {floor:,} mojos ({why})")
    if campaign.kind not in rules.kinds:
        raise NotRoutable(f"this router does not sign for {campaign.kind.name} campaigns")
    if campaign.claim_window < rules.min_claim_window:
        raise NotRoutable(f"claim window is under the {rules.min_claim_window} blocks this router signs for")
    if campaign.claim_window > rules.max_claim_window:
        raise NotRoutable(f"claim window is over the {rules.max_claim_window} blocks this router signs for")
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


def operator_fee(rules: "RouterRules", length: int, every: int) -> int:
    """What a campaign of `length` blocks prepays the operator for its roots: one post per `every`
    blocks (rounded up), plus the final root and one repost (EXTRA_POSTS), at `rules.post_fee` each."""
    if rules.post_fee <= 0 or length <= 0:
        return 0
    return rules.post_fee * (-(-length // max(1, every)) + EXTRA_POSTS)
