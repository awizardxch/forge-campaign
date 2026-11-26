"""One service hosting every distributor of this build and this host's keys (owner, 2026-11-11).

A distributor pays one reward asset, so the service hosts one per asset: XCH and every CAT a
creator has launched one for (flpcr.service.launch). Each hosted distributor keeps the
service's existing machinery unchanged (a `CampaignService`, its create intake, its claim
sequencer, its own singleton lease); the fleet finds them, checks them, decides which takes
creates, and runs every operator loop with the one operator key and the one router key.

Which distributors it hosts:

* the primary (FLPCR_LAUNCHER), a launcher pin; its Config and operator are the build pin's
  template (`BuildPin.from_config`), which must name this host's router and operator keys. With
  no primary (template mode: `primary=None`, `pin` given, `BuildPin.template`), the fleet starts
  empty and the first creator's launch of a reward asset is its first distributor;
* any other launcher configured by id (FLPCR_LAUNCHERS): hosted whatever it is, and takes
  creates only if it also passes the build pin;
* every launcher hinted with `BuildPin.hint` on chain (discovery), hosted only if it passes
  the build pin: resynced with this build's leaves, the pinned finalizer, the template's Config
  with this host's router, and this host's operator. Anything else is refused and remembered.

Which takes creates (the race rule, flpcr.build_pin.canonical_order): per reward asset, the
build-pinned distributor whose launcher confirmed first. Every hosted distributor gets roots,
claims and reclaims; a create quoted on any but the canonical one is answered 409 with the
canonical launcher id.

HTTP (flpcr.service.http): `/api/v1/...` is the primary, as before (template mode: XCH's
canonical distributor, once one is launched); `/d/<launcher>/api/v1/...`
one hosted distributor; `/a/<asset>/api/v1/...` an asset's canonical one; `/api/v1/fleet` the
list; `/api/v1/launch/quote`, `/api/v1/launch/create` and `/api/v1/launch/terms/<asset>` the
first-use launch.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from chia_wallet_sdk import Clvm, Constants, SecretKey, standard_puzzle_hash

from ..build_pin import BuildPin, NotPinned, canonical_order
from ..chain.driver import Puzzles
from ..chain.resync import ResyncError
from ..chain.signer import KeySigner
from ..registry import pins_fingerprint
from ..router import RouterRules
from .core import CampaignService, OperatorKey
from .intake import CampaignIntake, RouterCoins
from .launch import LaunchIntake
from .lease import LEASE_SECONDS
from .sequencer import ClaimSequencer

MAX_NEW_PER_PASS = 16  # discovered launchers tried per pass: hinting coins is free, booting is not


@dataclass
class Hosted:
    launcher_id: bytes
    service: CampaignService
    intake: CampaignIntake
    sequencer: ClaimSequencer
    asset: bytes = bytes(32)
    launched_at: int = 0  # the launcher coin's confirmed height
    build_pinned: bool = False
    why_not: Optional[str] = None  # why it takes no creates by the build pin, if it does not
    watcher: object = None


@dataclass
class Fleet:
    chain: object
    network: str
    primary: Optional[bytes]  # None: template mode, with `template` the build pin
    source_for: Callable
    operator: Optional[OperatorKey] = None
    router_sk: Optional[SecretKey] = None
    rules: RouterRules = field(default_factory=RouterRules)
    pools: Callable = lambda: {}
    cat_policies: Optional[Callable] = None
    every: int = 180
    manifests_dir: object = None
    registry: object = None
    configured: List[bytes] = field(default_factory=list)
    discover: bool = True
    lease_seconds: float = LEASE_SECONDS
    puzzles_factory: Callable = lambda: Puzzles(Clvm())
    service_factory: Optional[Callable] = None  # (launcher_id) -> CampaignService, for tests
    pins: Optional[str] = None  # the compiled build's fingerprint (default: pins_fingerprint())
    template: Optional[BuildPin] = None  # the build pin when there is no primary
    watcher_factory: Optional[Callable] = None  # (launcher_id) -> a Watcher for each hosted one

    def __post_init__(self) -> None:
        self.hosted: Dict[bytes, Hosted] = {}
        self.rejected: Dict[bytes, str] = {}
        self.pin: Optional[BuildPin] = None
        self.lock = threading.RLock()
        self.launches = LaunchIntake(self, self.lease_seconds)
        self.router_coins = RouterCoins()  # one router address, many coins: one per create in a block
        self.pins = self.pins or pins_fingerprint()

    # --- keys -----------------------------------------------------------------------------

    @property
    def router_ph(self) -> Optional[bytes]:
        return standard_puzzle_hash(self.router_sk.public_key()) if self.router_sk else None

    def sign_router(self, spends):
        return KeySigner.for_network([self.router_sk], self.network).sign(spends)

    def wait_for(self, coin_id: bytes, wait: float, poll: float) -> int:
        deadline = time.time() + wait
        while True:
            at = self.chain.confirmed_at(coin_id)
            if at:
                return at
            if time.time() > deadline:
                raise RuntimeError(f"not confirmed within {wait:.0f} s")
            time.sleep(poll)

    @property
    def booted(self) -> bool:
        """The build pin is read (and, with a primary, the primary is hosted)."""
        return self.pin is not None and (self.primary is None or self.primary in self.hosted)

    def home(self) -> Optional[Hosted]:
        """What the bare `/api/v1/...` paths answer for: the primary, or (template mode) XCH's
        canonical distributor; None before there is one."""
        if self.primary is not None:
            return self.hosted.get(self.primary)
        return self.for_asset(bytes(32))

    def terms(self, asset: bytes) -> dict:
        """What this router signs for in `asset`, with or without a distributor for it yet."""
        intake = CampaignIntake(None, self.pools, self.rules, router_sk=self.router_sk,
                                lease_seconds=self.lease_seconds, cat_policies=self.cat_policies)
        return intake.terms(asset)

    # --- hosting --------------------------------------------------------------------------

    def _service(self, launcher_id: bytes) -> CampaignService:
        if self.service_factory is not None:
            return self.service_factory(launcher_id)
        return CampaignService(self.chain, launcher_id, self.network, self.source_for, operator=self.operator,
                               every=self.every, manifests_dir=self.manifests_dir, registry=self.registry,
                               puzzles_factory=self.puzzles_factory)

    def _host(self, launcher_id: bytes, service: CampaignService) -> Hosted:
        d = service.d
        state = self.chain.reader.coin_state(launcher_id)
        h = Hosted(launcher_id, service, None, ClaimSequencer(service), asset=d.cfg.reward_asset,
                   launched_at=state.created_height if state is not None else 0)
        try:
            if self.pin is None:
                raise NotPinned("no build pin")
            self.pin.check(service.s.resynced, pins=self.pins)
            h.build_pinned = True
        except NotPinned as e:
            h.why_not = str(e)
        h.intake = CampaignIntake(service, self.pools, self.rules, router_sk=self.router_sk,
                                  lease_seconds=self.lease_seconds, cat_policies=self.cat_policies,
                                  gate=lambda lid=launcher_id: self.gate(lid), router_coins=self.router_coins)
        if self.watcher_factory is not None:
            h.watcher = self.watcher_factory(launcher_id)
        return h

    def _check_keys(self, pin: BuildPin, what: str) -> None:
        if self.router_sk is not None and pin.router_ph != self.router_ph:
            raise ValueError(f"{what}'s router is not this host's router key")
        if self.operator is not None and pin.operator_ph != self.operator.puzzle_hash:
            raise ValueError(f"{what}'s operator is not this host's operator key")

    def boot(self) -> None:
        """The primary first (its Config is the build pin), then every configured launcher, then
        discovery. A configured launcher that cannot be resynced is reported and skipped. With no
        primary the build pin is `template`, which must name this host's two keys."""
        if self.primary is None:
            if self.template is None:
                raise ValueError("no primary distributor and no template build pin")
            if self.template.network != self.network:
                raise ValueError("the template build pin is another network's")
            if self.template.pins != self.pins:
                raise ValueError("the template build pin names another compiled build")
            self._check_keys(self.template, "the template build pin")
            with self.lock:
                self.pin = self.template
        else:
            svc = self._service(self.primary)
            svc.boot()
            got = svc.s.resynced
            pin = BuildPin.from_config(self.network, got.config, got.distributor.chain_state.engine_ph, self.pins)
            self._check_keys(pin, "the primary distributor")
            with self.lock:
                self.pin = pin
                self.hosted[self.primary] = self._host(self.primary, svc)
        for lid in self.configured:
            if lid not in self.hosted:
                try:
                    self.adopt(lid, configured=True)
                except Exception as e:  # noqa: BLE001
                    self.rejected[lid] = f"{type(e).__name__}: {e}"
        if self.discover:
            self.discover_new()

    def adopt(self, launcher_id: bytes, configured: bool = False) -> Hosted:
        """Host a distributor: resync it from chain and check it against the build pin. A
        discovered one (not configured by id) that fails the pin is refused, not hosted."""
        with self.lock:
            if launcher_id in self.hosted:
                return self.hosted[launcher_id]
        svc = self._service(launcher_id)
        svc.boot()
        h = self._host(launcher_id, svc)
        if not h.build_pinned and not configured:
            raise NotPinned(h.why_not or "not the pinned build")
        with self.lock:
            self.hosted.setdefault(launcher_id, h)
            self.rejected.pop(launcher_id, None)
            return self.hosted[launcher_id]

    def discover_new(self) -> List[bytes]:
        """Launchers hinted for this host that are not hosted or refused yet."""
        if self.pin is None or not hasattr(self.chain, "hinted"):
            return []
        added = []
        tried = 0
        launcher_hash = Constants.singleton_launcher_hash()
        for coin in self.chain.hinted(self.pin.hint):
            if coin.puzzle_hash != launcher_hash or coin.amount != 1:
                continue
            lid = coin.coin_id()
            if lid in self.hosted or lid in self.rejected:
                continue
            if tried >= MAX_NEW_PER_PASS:
                break
            tried += 1
            try:
                self.adopt(lid)
                added.append(lid)
            except ResyncError as e:
                if "never spent" not in str(e):  # an unspent eve may still be initialized: try again later
                    self.rejected[lid] = f"resync: {e}"
            except NotPinned as e:
                self.rejected[lid] = str(e)
            except Exception as e:  # noqa: BLE001 -- a node error: try again next pass
                traceback.print_exc()
        return added

    # --- which distributor takes creates ----------------------------------------------------

    def canonical(self, asset: bytes) -> Optional[bytes]:
        with self.lock:
            pinned = [(h.launcher_id, h.launched_at) for h in self.hosted.values() if h.build_pinned and h.asset == asset]
        order = canonical_order(pinned)
        return order[0] if order else None

    def gate(self, launcher_id: bytes) -> Optional[bytes]:
        """None if `launcher_id` takes new campaigns, else the launcher id that does (or itself,
        when nothing does: a distributor that is not the pinned build never takes creates)."""
        h = self.hosted.get(launcher_id)
        if h is None:
            return launcher_id
        best = self.canonical(h.asset)
        if best == launcher_id:
            return None
        return best or launcher_id

    def get(self, launcher_id: bytes) -> Optional[Hosted]:
        return self.hosted.get(launcher_id)

    def for_asset(self, asset: bytes) -> Optional[Hosted]:
        best = self.canonical(asset)
        return self.hosted.get(best) if best else None

    # --- reads ----------------------------------------------------------------------------

    def describe(self) -> dict:
        with self.lock:
            hosted = list(self.hosted.values())
        rows = []
        for h in sorted(hosted, key=lambda x: (x.asset != bytes(32), x.asset, x.launched_at, x.launcher_id)):
            st = h.service.s
            rows.append({
                "launcher_id": h.launcher_id.hex(), "reward_asset": h.asset.hex(), "launched_at": h.launched_at,
                "build_pinned": h.build_pinned, "why_not": h.why_not,
                "canonical": self.canonical(h.asset) == h.launcher_id,
                "creates": self.gate(h.launcher_id) is None and self.router_sk is not None,
                "api": f"/d/{h.launcher_id.hex()}", "booted": st is not None,
                "campaigns": len(st.resynced.distributor.chain_state.campaigns) if st else None,
                "coin": st.resynced.distributor.coin.coin_id().hex() if st else None,
            })
        try:
            peak = self.chain.peak()
        except Exception:  # noqa: BLE001 -- a node hiccup: the list still answers
            peak = None
        return {"network": self.network, "primary": self.primary.hex() if self.primary else None, "peak": peak,
                "template": self.primary is None, "build": self.pin.to_json() if self.pin else None,
                "router_ready": self.router_sk is not None, "operator_ready": self.operator is not None,
                "distributors": rows, "refused": {k.hex(): v for k, v in self.rejected.items()}}

    # --- the loop -------------------------------------------------------------------------

    def step(self, discover: bool = True, log=print) -> Dict[str, object]:
        """One pass: discovery, then each hosted distributor's follow, root and claim batch. A fault
        in one distributor never stops the others."""
        out: Dict[str, object] = {}
        if discover and self.discover:
            try:
                for lid in self.discover_new():
                    log(f"fleet: hosting {lid.hex()[:12]}…")
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        with self.lock:
            hosted = list(self.hosted.values())
        for h in hosted:
            key = h.launcher_id.hex()
            try:
                svc = h.service
                if svc.follow():
                    log(f"{key[:12]}…: resynced after another builder's spend")
                elif svc.s.mismatch and int(time.time()) % 10 == 0:
                    svc.boot()
                posted = svc.operator_step()
                if posted:
                    log(f"{key[:12]}…: posted root {posted['root_id']} [{posted['start']}, {posted['end']}) fee {posted['fee']:,}")
                paid = h.sequencer.run_batch()
                if paid:
                    log(f"{key[:12]}…: claims {paid}")
                out[key] = {"posted": posted, "claims": paid}
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                out[key] = {"error": f"{type(e).__name__}: {e}"}
                try:
                    h.service.boot()
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        return out
