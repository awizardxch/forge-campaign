"""Pinning a distributor by its build, not by its launcher id (owner, 2026-11-14).

Every reward asset needs its own distributor (the asset is curried into the puzzles), and a
new CAT's distributor is launched by its first campaign's creator, so the page and the service
cannot list launcher ids ahead of time. They accept a distributor instead when the chain shows
it is THIS build with THIS host's keys:

* the leaves are this build's (`resync` already refuses any other: it rebuilds the Config from
  the first spend and replays every generation with these compiled leaves);
* it runs the pinned reserve finalizer (audit B2 CP-06), not upstream's;
* every Config field but the launcher id and the reward asset is the template's: the router,
  guardian, admin and dev fee address, the fee, the clocks, the bounds and the bond;
* its operator (the state's engine, and any pending rotation) is the host's operator;
* its reward asset is the one asked for.

`BuildPin` is that template. The service takes it from its primary distributor (FLPCR_LAUNCHER,
itself a launcher pin) and checks it against its own router and operator keys; the site keeps
its own copy in `src/lib/campaigns/pins.ts` and rebuilds the puzzle hashes from it.

Canonical distributor: one asset may end up with more than one pinned distributor (two hosts
launching at once, or a launch that confirmed late). The canonical one is the one whose
launcher coin confirmed first (lowest height, then lowest launcher id). Only the canonical one
takes new campaigns; every pinned one is still hosted (roots, claims, reclaims), so a deposit
on a late one is paid out like any other.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, fields
from typing import Iterable, List, Optional, Sequence, Tuple

from .distributor import Config

HINT_TAG = b"flpcr-distributor-v1"
TEMPLATE_FIELDS = ("dev_fee_ph", "admin_ph", "guardian_ph", "router_ph", "fee_bps", "dispute_blocks",
                   "rotate_delay", "delegate_fee", "min_campaign_length", "max_campaign_length",
                   "max_campaigns", "dispute_bond")


class NotPinned(ValueError):
    """The distributor on chain is not this build with this host's keys."""


@dataclass(frozen=True)
class BuildPin:
    network: str
    operator_ph: bytes
    dev_fee_ph: bytes
    admin_ph: bytes
    guardian_ph: bytes
    router_ph: bytes
    fee_bps: int
    dispute_blocks: int
    rotate_delay: int
    delegate_fee: int
    min_campaign_length: int
    max_campaign_length: int
    max_campaigns: int
    dispute_bond: int
    pins: str  # pins_fingerprint() of the compiled build

    @classmethod
    def from_config(cls, network: str, cfg: Config, operator_ph: bytes, pins: str) -> "BuildPin":
        return cls(network, operator_ph, pins=pins, **{f: getattr(cfg, f) for f in TEMPLATE_FIELDS})

    def config_for(self, launcher_id: bytes, reward_asset: bytes) -> Config:
        """The Config a distributor of this build pays `reward_asset` with, launched as `launcher_id`."""
        return Config(launcher_id, reward_asset, **{f: getattr(self, f) for f in TEMPLATE_FIELDS})

    @property
    def hint(self) -> bytes:
        """The hint a launch puts on its launcher coin, so a host finds every distributor launched
        for it from the chain alone (get_coin_records_by_hint). Anyone can hint a coin: a hinted
        launcher is only a candidate, checked like any other."""
        return hashlib.sha256(HINT_TAG + self.router_ph + self.operator_ph).digest()

    def to_json(self) -> dict:
        out = {k: (v.hex() if isinstance(v, bytes) else v) for k, v in asdict(self).items()}
        out["hint"] = self.hint.hex()
        return out

    # --- the check ------------------------------------------------------------------------

    def check(self, resynced, reward_asset: Optional[bytes] = None, pins: Optional[str] = None) -> None:
        """Raise NotPinned unless the resynced distributor is this build with this host's keys
        (and pays `reward_asset`, when given). `pins` is the fingerprint of the compiled folder
        the resync ran with: resync proves the leaves are that folder's."""
        from .chain.driver import FINALIZER_PINNED

        d, cfg = resynced.distributor, resynced.config
        if pins is not None and pins != self.pins:
            raise NotPinned("wrong build: resynced with another compiled build than the pinned one")
        if d.finalizer_kind != FINALIZER_PINNED:
            raise NotPinned("wrong build: it runs upstream's reserve finalizer, not the pinned one (CP-06)")
        if cfg.router_ph != self.router_ph:
            raise NotPinned(f"wrong router: {cfg.router_ph.hex()[:12]}…, this host's is {self.router_ph.hex()[:12]}…")
        st = d.chain_state
        if st.engine_ph != self.operator_ph:
            raise NotPinned(f"wrong operator: {st.engine_ph.hex()[:12]}…, this host's is {self.operator_ph.hex()[:12]}…")
        if st.rotation is not None and st.rotation.engine_ph != self.operator_ph:
            raise NotPinned("wrong operator: a rotation to another operator is pending")
        for name in TEMPLATE_FIELDS:
            if name != "router_ph" and getattr(cfg, name) != getattr(self, name):
                raise NotPinned(f"wrong config: {name} is not the pinned build's")
        if reward_asset is not None and cfg.reward_asset != reward_asset:
            raise NotPinned("wrong asset: it pays another reward asset")
        # Born empty. The launch, not a leaf, chooses the eve's State, so a launch of this exact
        # build could start with campaign rows (or a reserve, roots, a rotation) already in it, and
        # a row naming the launcher as creator would let them reclaim from the reserve that honest
        # creators' deposits fill (2026-11-14 conformance review, part B, F-B1). Every launch this
        # host or the site makes starts at ChainState.genesis (launch.py, src/lib/campaigns/build.ts
        # genesisStateHash), so anything else is refused, and a refused launch is never an asset's
        # canonical distributor (fleet.canonical counts only build-pinned ones).
        from .chain.state import ChainState
        from .clvm import sha256tree

        born = getattr(resynced, "initial_state_hash", None)
        if born is None:
            raise NotPinned("not born empty: the distributor's first State is unknown")
        # Byte-exact, as the site checks it (build.ts genesisStateHash): the State a launch of this
        # host or the site builds (driver.Distributor: ChainState.genesis(...).to_value()).
        if bytes(born) != bytes(sha256tree(ChainState.genesis(self.operator_ph).to_value())):
            raise NotPinned("not born empty: it was launched with a State other than the empty genesis")


def canonical_order(candidates: Iterable[Tuple[bytes, int]]) -> List[bytes]:
    """Launcher ids, canonical first: (launcher id, launcher's confirmed height) sorted by height,
    then by launcher id. The race rule: the first launch to confirm is the asset's distributor."""
    return [lid for lid, _ in sorted(candidates, key=lambda x: (x[1], x[0]))]


def pin_fields() -> Sequence[str]:
    return [f.name for f in fields(BuildPin)]
