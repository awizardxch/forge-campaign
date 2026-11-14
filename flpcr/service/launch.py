"""First-use launch: a creator's campaign that also launches its reward asset's distributor.

Owner rule (2026-11-11): every CAT trading on the Forge may be a reward, one distributor per
reward asset, and the cost of launching a CAT's distributor is paid by its first creator, never
by the operator. No puzzle changes: a distributor is launched, initialized and given its first
campaign in ONE bundle, which the creator funds and the router co-signs exactly as it co-signs
any create.

The bundle (every spend but the creator's and the router's is built here):

    creator's XCH coin (the "funding coin")   CREATE_COIN launcher 1 (hinted, BuildPin.hint)
                                              CREATE_COIN reserve 0        (XCH reward only)
                                              ASSERT_COIN_ANNOUNCEMENT the launcher's
                                              SEND_MESSAGE 23 "k" + id -> the eve coin
                                              change, RESERVE_FEE >= network_fee
                                              (an XCH reward's deposit is the coins' spare value)
    creator's first CAT coin (the "seed")     CREATE_COIN reserve 0 (hinted)  (CAT reward only)
                                              change; it and the other CAT coins leave exactly
                                              the deposit in the reserve's ring
    launcher                                  the eve singleton
    eve singleton                             init, create
    reserve                                   0 -> the budget; the 3% fee to the dev address
    router coin                               "c" + id -> the eve coin, recreated whole

The launcher id is the funding coin's child, and the pinned reserve finalizer curries the
reserve's parent (the funding coin for XCH, the seed for a CAT), so the quote names both coins
and the eve coin the creator's message goes to is fixed before anything is signed. The page
rebuilds that eve coin from its own pins (chia-cfmm src/lib/campaigns/build.ts) and refuses a
quote whose message goes anywhere else.

Race rule (flpcr.build_pin): launch quotes are free and many may be open for one asset. The
first signed launch to arrive is pushed; while it waits to confirm, any other launch for that
asset is told to retry, and once it has confirmed, every other launch quote for that asset is
answered 409 with the new distributor's launcher id, so the creator re-quotes there as an
ordinary create (a new signature over the same deposit). A bundle the router has not co-signed
cannot be pushed by anyone, so a losing creator's coins are never spent. Should two launches
confirm anyway (two hosts, or a dropped push that landed late), both distributors are hosted
and paid out; the one that confirmed first is canonical and takes every later campaign.
"""

from __future__ import annotations

import secrets
import threading
import time
import types
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from chia_wallet_sdk import Cat, CatSpend, Clvm, Coin, Constants, SecretKey, Signature, Spend, cat_puzzle_hash, standard_puzzle_hash

from ..chain.driver import HEIGHT_WINDOW, Distributor, Puzzles
from ..chain.values import to_bytes, to_int, to_list, to_program
from ..router import XCH, NotRoutable
from .chain import PushRefused
from .core import FEE_PER_COST, MIN_FEE
from .http import puzzle_hash_of
from .intake import H_MARGIN, MODE_CREATE, CampaignIntake, Moved, Quote, Requote, _hex
from .lease import LEASE_SECONDS, LeaseBusy, SingletonLease

OP_CREATE_COIN = 51
OP_ASSERT_COIN_ANNOUNCEMENT = 61


@dataclass
class LaunchQuote:
    quote: Quote
    d: Distributor
    funding: Coin
    seed: Optional[Cat]
    eve: Coin
    intake: CampaignIntake  # a create intake over the distributor-to-be
    launch: dict = field(default_factory=dict)


def launcher_announcement(launcher_id: bytes, eve_ph: bytes) -> bytes:
    """What the funding coin asserts: the launcher's CREATE_COIN_ANNOUNCEMENT of (eve_ph 1 ())."""
    from hashlib import sha256

    clvm = Clvm()
    return sha256(launcher_id + to_program(clvm, [eve_ph, 1, None]).tree_hash()).digest()


class LaunchIntake:
    def __init__(self, fleet, lease_seconds: float = LEASE_SECONDS, clock=time.time):
        self.fleet = fleet
        self.lease_seconds = lease_seconds
        self.clock = clock
        self.quotes: Dict[str, LaunchQuote] = {}
        self.pushing: Dict[bytes, str] = {}  # reward asset -> the launch quote pushed and waiting
        self.lock = threading.Lock()

    # --- reading the creator's coins ------------------------------------------------------

    def _funding(self, body: dict, creator: bytes) -> Coin:
        state = self.fleet.chain.reader.coin_state(_hex(body.get("funding_coin") or ""))
        if state is None or state.spent_height is not None:
            raise ValueError("funding_coin is not an unspent coin on chain")
        if state.coin.puzzle_hash != creator:
            raise ValueError("funding_coin is not at the creator's address: the coin that launches the "
                             "distributor also sends the creator's consent")
        return state.coin

    def _seed(self, body: dict, creator: bytes, asset: bytes, clvm: Clvm) -> Cat:
        """The creator's CAT coin the reserve is born from, with the lineage its spend needs."""
        reader = self.fleet.chain.reader
        seed_id = _hex(body.get("seed_coin") or "")
        state = reader.coin_state(seed_id)
        if state is None or state.spent_height is not None:
            raise ValueError("seed_coin is not an unspent coin on chain")
        if state.coin.puzzle_hash != cat_puzzle_hash(asset, creator):
            raise ValueError("seed_coin is not a coin of the reward CAT at the creator's address")
        parent = reader.coin_state(state.coin.parent_coin_info)
        parent_spend = reader.coin_spend(state.coin.parent_coin_info)
        if parent is None or parent_spend is None:
            raise ValueError("seed_coin's parent is not on chain")
        puzzle = clvm.deserialize(parent_spend.puzzle_reveal).puzzle()
        cats = puzzle.parse_child_cats(parent.coin, clvm.deserialize(parent_spend.solution)) or []
        match = [c for c in cats if c.coin.coin_id() == seed_id]
        if not match:
            raise ValueError("seed_coin is not a CAT child of its parent's spend")
        return match[0]

    # --- quote ----------------------------------------------------------------------------

    def quote(self, body: dict) -> dict:
        fleet = self.fleet
        pin = fleet.pin
        if pin is None:
            raise ValueError("booting: the build pin is not read yet")
        if fleet.router_sk is None:
            raise ValueError("this host does not hold the router key: it cannot launch a distributor")
        asset = _hex(body.get("reward_asset") or "")
        if len(asset) != 32:
            raise ValueError("reward_asset is a 32-byte asset id (00…00 for XCH)")
        canonical = fleet.canonical(asset)
        if canonical is not None:
            raise Moved(f"this reward asset already has a distributor: quote a create on {canonical.hex()}", canonical)
        creator = puzzle_hash_of(str(body.get("creator") or ""))
        h = fleet.chain.peak()
        puzzles = fleet.puzzles_factory()
        funding = self._funding(body, creator)
        launcher_id = Coin(funding.coin_id(), Constants.singleton_launcher_hash(), 1).coin_id()
        d = Distributor(puzzles, pin.config_for(launcher_id, asset), pin.operator_ph)
        seed = None
        if d.is_cat:
            seed = self._seed(body, creator, asset, puzzles.clvm)
            d.seed_reserve(seed)
        d.launcher_spend(funding)  # sets the eve coin, its lineage and (XCH) the reserve
        shim = types.SimpleNamespace(d=d, chain=fleet.chain, network=fleet.network, lease=SingletonLease(self.clock))
        intake = CampaignIntake(shim, fleet.pools, fleet.rules, router_sk=fleet.router_sk,
                                lease_seconds=self.lease_seconds, cat_policies=fleet.cat_policies)
        campaign = intake.campaign_from(body, h)
        try:
            intake.check_route(campaign, h)
        except NotRoutable as e:
            raise ValueError(f"not routable: {e}") from e
        qid = "L" + secrets.token_hex(8)
        q = Quote(qid, campaign, h, d.coin.coin_id(), self.clock() + self.lease_seconds)
        lq = LaunchQuote(q, d, funding, seed, d.coin, intake)
        q.network_fee = self.estimate_network_fee(lq)
        lq.launch = {
            "launcher_id": launcher_id.hex(), "funding_coin": funding.coin_id().hex(),
            "seed_coin": seed.coin.coin_id().hex() if seed else None,
            "reserve_parent": d.reserve_coin.parent_coin_info.hex(), "reserve_puzzle_hash": d.reserve_ph.hex(),
            "eve_coin": d.coin.coin_id().hex(), "eve_puzzle_hash": d.coin.puzzle_hash.hex(),
            "hint": pin.hint.hex(), "launcher_announcement": launcher_announcement(launcher_id, d.coin.puzzle_hash).hex(),
            "config": {k: (v.hex() if isinstance(v, bytes) else v) for k, v in zip(_CONFIG_NAMES, d.config_value())},
            "operator_ph": pin.operator_ph.hex(), "launch_mojos": 1,
        }
        with self.lock:
            self._prune()
            self.quotes[qid] = lq
        return self.describe(lq)

    def describe(self, lq: LaunchQuote) -> dict:
        out = lq.intake.describe(lq.quote)
        conditions = out["conditions"]
        conditions["launch"] = {
            "funding": [[OP_CREATE_COIN, Constants.singleton_launcher_hash().hex(), 1, [lq.launch["hint"]]],
                        *([[OP_CREATE_COIN, lq.launch["reserve_puzzle_hash"], 0]] if not lq.d.is_cat else []),
                        [OP_ASSERT_COIN_ANNOUNCEMENT, lq.launch["launcher_announcement"]]],
            "seed": ([[OP_CREATE_COIN, lq.launch["reserve_puzzle_hash"], 0, [lq.launch["reserve_puzzle_hash"]]]]
                     if lq.d.is_cat else None),
        }
        out["launch"] = lq.launch
        out["lease"] = {"expires_at": int(lq.quote.expires_at), "seconds": int(self.lease_seconds)}
        return out

    _STAND_IN = SecretKey.from_seed(bytes([0x5f]) * 32).derive_synthetic()

    def estimate_network_fee(self, lq: LaunchQuote) -> int:
        """The launch bundle's network fee from a stand-in of it: the funding coin, the launcher,
        the eve's init + create (with a stand-in CAT ring for a CAT), the router; a quarter over,
        rounded up to a millionth of an XCH (as CampaignIntake.estimate_network_fee)."""
        d, c, q = lq.d, lq.quote.campaign, lq.quote
        clvm = Clvm()
        pk = self._STAND_IN.public_key()
        ph = standard_puzzle_hash(pk)
        router_ph = self.fleet.pin.router_ph
        launch = Constants.singleton_launcher_hash()
        conds = [[66, MODE_CREATE, b"k" + c.id, q.coin_id], [OP_CREATE_COIN, launch, 1, [bytes(32)]],
                 [OP_CREATE_COIN, d.reserve_ph, 0], [OP_ASSERT_COIN_ANNOUNCEMENT, bytes(32)],
                 [OP_CREATE_COIN, ph, 10**12], [52, MIN_FEE]]
        clvm.spend_standard_coin(Coin(bytes([1]) * 32, ph, c.amount + 10**12), pk,
                                 clvm.delegated_spend([to_program(clvm, x) for x in conds]))
        clvm.spend_standard_coin(Coin(bytes(32), router_ph, 1), pk, clvm.delegated_spend(
            [to_program(clvm, [66, MODE_CREATE, b"c" + c.id, q.coin_id]), to_program(clvm, [51, router_ph, 1, [router_ph]])]))
        stand_in = list(clvm.coin_spends())
        mark = d._mark()
        try:
            cats = []
            if d.is_cat:
                dc = d.clvm
                inner = dc.standard_spend(pk, dc.delegated_spend([to_program(dc, [51, d.reserve_ph, 0, [d.reserve_ph]]),
                                                                  to_program(dc, [51, ph, lq.seed.coin.amount - c.amount, [ph]])]))
                cats = [CatSpend(lq.seed, inner)]
            calls = [d.init(q.h), d.create(q.h, c.creator, c.amount, c.start, c.end, int(c.kind), c.target,
                                           c.params.as_clvm(), c.nonce, c.claim_window, router=router_ph)]
            singleton = list(d.prepare(calls, cats).spends)
            cost = d.spend_cost(singleton + stand_in) + d.spend_cost([d.launcher_spend(lq.funding)])
        except Exception:  # noqa: BLE001 -- size it from the stand-in alone
            cost = d.spend_cost(stand_in) * 6
        finally:
            d._rewind(mark)
        fee = max(MIN_FEE, FEE_PER_COST * cost) * 5 // 4
        return -(-fee // 1_000_000) * 1_000_000

    def _prune(self) -> None:
        cutoff = self.clock() - 3600
        for k in [k for k, v in self.quotes.items() if v.quote.expires_at < cutoff and v.quote.status in ("quoted", "failed")]:
            del self.quotes[k]

    # --- create ---------------------------------------------------------------------------

    def _conditions(self, clvm: Clvm, spend) -> List[list]:
        puzzle, solution = clvm.deserialize(spend.puzzle_reveal), clvm.deserialize(spend.solution)
        parsed = puzzle.puzzle().parse_cat(spend.coin, solution)
        if parsed is not None:
            puzzle, solution = parsed.p2_puzzle.program, parsed.p2_solution
        out = []
        for cond in to_list(puzzle.run(solution, 11_000_000_000, False).value):
            items = to_list(cond) if cond.is_pair() else []
            if items and items[0].is_atom():
                out.append(items)
        return out

    def check_launch(self, lq: LaunchQuote, spends) -> None:
        """The launch's own part of the creator's spends: the funding coin makes the launcher (and
        an XCH reserve) and asserts the launcher's announcement; a CAT seed makes the reserve."""
        clvm = Clvm()
        d = lq.d
        by_id = {s.coin.coin_id(): s for s in spends}
        funding = by_id.get(lq.funding.coin_id())
        if funding is None:
            raise ValueError("the quoted funding coin is not spent in the bundle")
        launcher_hash = Constants.singleton_launcher_hash()
        made = [(s.coin.coin_id(), it) for s in spends for it in self._conditions(clvm, s)
                if to_int(it[0]) == OP_CREATE_COIN and to_bytes(it[1]) == launcher_hash]
        if len(made) != 1 or made[0][0] != lq.funding.coin_id() or to_int(made[0][1][2]) != 1:
            raise ValueError("exactly one coin, the funding coin, must create the singleton launcher, with 1 mojo")
        memos = to_list(made[0][1][3]) if len(made[0][1]) > 3 and made[0][1][3].is_pair() else []
        if not memos or to_bytes(memos[0]) != self.fleet.pin.hint:
            raise ValueError("the launcher must carry this host's hint, so the distributor is found from chain")
        conds = self._conditions(clvm, funding)
        ann = bytes.fromhex(lq.launch["launcher_announcement"])
        if not any(to_int(it[0]) == OP_ASSERT_COIN_ANNOUNCEMENT and to_bytes(it[1]) == ann for it in conds):
            raise ValueError("the funding coin must assert the launcher's announcement of the quoted eve coin")
        reserve = [(s.coin.coin_id(), it) for s in spends for it in self._conditions(clvm, s)
                   if to_int(it[0]) == OP_CREATE_COIN and to_bytes(it[1]) == d.reserve_ph]
        parent = d.reserve_coin.parent_coin_info
        if len(reserve) != 1 or reserve[0][0] != parent or to_int(reserve[0][1][2]) != 0:
            raise ValueError("exactly one coin, the quoted reserve parent, must create the empty reserve")

    def create(self, body: dict, wait: float = 1800, poll: float = 15) -> dict:
        from .sequencer import _bundle

        fleet = self.fleet
        if fleet.router_sk is None:
            raise ValueError("this host does not hold the router key: it cannot launch a distributor")
        with self.lock:
            lq = self.quotes.get(str(body.get("quote") or ""))
        if lq is None:
            raise Requote("unknown or expired launch quote; re-quote")
        q, d, intake = lq.quote, lq.d, lq.intake
        asset = d.cfg.reward_asset
        if q.status != "quoted":
            raise ValueError(f"this quote is already {q.status}")
        if self.clock() >= q.expires_at:
            raise Requote("the launch quote lapsed; re-quote and sign again")
        canonical = fleet.canonical(asset)
        if canonical is not None:  # another launch won the race: quote an ordinary create there
            raise Moved(f"this reward asset's distributor was launched meanwhile: quote a create on {canonical.hex()}",
                        canonical)
        with self.lock:
            other = self.pushing.get(asset)
            if other is not None and other != q.id:
                raise LeaseBusy("another launch for this reward asset is being confirmed; retry shortly", 30)
            self.pushing[asset] = q.id
        pushed = False
        router_coin = None
        try:
            h = fleet.chain.peak()
            if h >= q.h + HEIGHT_WINDOW - H_MARGIN:
                raise Requote("the quote's height is too old for the launch spend; re-quote")
            spends, sig = _bundle(Clvm(), body.get("spend_bundle"))
            xch, cats, _, reserved = intake.check_bundle(q, spends, sig)
            self.check_launch(lq, spends)
            c = q.campaign
            if d.is_cat:  # the reserve's lineage from the seed exactly as the bundle spends it
                seed = [x.cat for x in cats if x.cat.coin.coin_id() == lq.seed.coin.coin_id()]
                if not seed:
                    raise ValueError("the quoted seed coin is not in the bundle's CAT spends")
                d.seed_reserve(seed[0])
            launcher = d.launcher_spend(lq.funding)
            calls = [d.init(q.h), d.create(q.h, c.creator, c.amount, c.start, c.end, int(c.kind), c.target,
                                           c.params.as_clvm(), c.nonce, c.claim_window, router=fleet.pin.router_ph)]
            prepared = d.prepare(calls, cats)
            mine = {s.coin.coin_id() for s in spends}
            coins = sorted((x for x in fleet.chain.unspent_at([fleet.pin.router_ph]) if x.coin_id() not in mine),
                           key=lambda x: x.amount)
            if not coins:
                raise ValueError("the router address holds no coin; the host must send it one")
            router_coin = fleet.router_coins.take(coins)
            router = intake._router_spend(router_coin, q.coin_id, c.id)
            all_spends = [launcher, *prepared.spends, *xch, *router]
            need = max(MIN_FEE, FEE_PER_COST * d.spend_cost(all_spends))
            if reserved < need:
                raise ValueError(f"the creator's spends reserve {reserved:,} mojos for the network fee; this launch needs "
                                 f"{need:,} (the quote's network_fee is {q.network_fee:,})")
            signature = Signature.aggregate([sig, fleet.sign_router(router)])
            successor = d.successor_coin(prepared)
            try:
                fleet.chain.push(all_spends, signature)
            except PushRefused as e:
                q.status, q.detail = "failed", str(e)
                raise ValueError(f"the chain refused the launch: {e}") from e
            pushed = True
            q.status = "pushed"
        finally:
            if not pushed:
                fleet.router_coins.release(router_coin)
                with self.lock:
                    if self.pushing.get(asset) == q.id:
                        del self.pushing[asset]
        try:
            at = fleet.wait_for(successor.coin_id(), wait, poll)
        except RuntimeError as e:
            fleet.router_coins.release(router_coin)
            q.status, q.detail = "failed", f"not confirmed: {e}"
            with self.lock:
                self.pushing.pop(asset, None)
            raise ValueError(f"the launch was pushed but not confirmed: {e}") from e
        fleet.router_coins.release(router_coin)
        try:
            fleet.adopt(d.launcher_id)  # boots it from chain: the same check as any discovered one
        finally:
            with self.lock:
                self.pushing.pop(asset, None)
        q.status, q.height = "created", at
        return {"quote": q.id, "campaign": c.id.hex(), "status": q.status, "height": at, "network_fee": reserved,
                "budget": c.budget, "fee": c.fee, "distributor": d.launcher_id.hex()}


_CONFIG_NAMES = ["distributor_id", "reward_asset", "fee_bps", "dev_fee_ph", "admin_ph", "guardian_ph", "router_ph",
                 "dispute_blocks", "rotate_delay", "delegate_fee", "min_campaign_length", "max_campaign_length",
                 "max_campaigns", "slot_1st_hash", "delegation_1st_hash", "dispute_bond", "bond_ph", "bond_launcher_hash"]
