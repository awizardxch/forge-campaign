"""One service hosting a distributor per reward asset (flpcr.service.fleet), on the simulator:

* the build pin (flpcr.build_pin): a distributor is accepted when the chain shows this build with
  this host's router and operator and the asked-for asset; wrong build, router, operator, config
  or asset is refused, by the check and by discovery;
* first-use launch (flpcr.service.launch): a CAT with no distributor gets one in its first
  campaign's own bundle, paid by the creator;
* the race rule: the first launch to confirm is the asset's distributor; a later launch quote is
  sent there as an ordinary create, with its coins untouched; a second distributor that confirms
  anyway is hosted and paid out but takes no creates;
* several campaigns on one pool: two creators back to back on one CAT, and two CATs in one block;
* the fleet's loop: roots for every distributor with the one operator key, claims, and a cold
  restart that finds every launched distributor from the chain alone.
"""

import dataclasses
import json
import threading
import time
import types
import unittest
import urllib.request

try:
    from chia_wallet_sdk import (Cat, CatInfo, CatSpend, Clvm, Coin, CoinSpend, Constants, LineageProof, SecretKey, Signature,
                                 Spend, SpendBundle, cat_puzzle_hash, standard_puzzle_hash)

    from flpcr.build_pin import BuildPin, NotPinned, canonical_order
    from flpcr.chain.driver import FINALIZER_UPSTREAM, HEIGHT_WINDOW, Distributor, Puzzles
    from flpcr.chain.resync import resync
    from flpcr.chain.signer import KeySigner
    from flpcr.chain.sim import SimWorld
    from flpcr.chain.values import to_program
    from flpcr.chain.state import ChainState
    from flpcr.clvm import sha256tree
    from flpcr.registry import pins_fingerprint
    from flpcr.service.chain import SimChain, bundle_json
    from flpcr.service.core import OperatorKey
    from flpcr.service.fleet import Fleet
    from flpcr.service.http import PostLimit, serve
    from flpcr.service.intake import Moved, PoolInfo, Requote
    from flpcr.service.lease import LeaseBusy
    from flpcr.service.sequencer import FEE_PER_CLAIM

    HAVE_CHAIN = True
except (ImportError, FileNotFoundError):  # pragma: no cover
    HAVE_CHAIN = False

from flpcr import FixtureSource, HolderEvent
from flpcr.router import XCH, RouterRules

POOL, LP = b"\xb0" * 32, b"\x1b" * 32
ASSET_A, ASSET_B = b"\xca" * 32, b"\xcb" * 32
EXTRA = bytes.fromhex("37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615")
LAUNCHER = None if not HAVE_CHAIN else Constants.singleton_launcher_hash()


class HeldChain(SimChain):
    """A simulator chain whose pushes wait for `flush`, so several bundles land in one block."""

    def __init__(self, sim):
        super().__init__(sim)
        self.hold = False
        self.held = []

    def push(self, spends, signature):
        if not self.hold:
            return super().push(spends, signature)
        self.held.append((list(spends), signature))
        return {"success": True}

    def flush(self):
        spends = [s for x in self.held for s in x[0]]
        sig = Signature.aggregate([x[1] for x in self.held])
        self.held = []
        self.sim.new_transaction(SpendBundle(spends, sig))


class FleetWorld:
    """An XCH primary distributor with the host's router and operator, a booted fleet over it, and
    creators holding XCH and two CATs."""

    def __init__(self, chain_cls=None, discover=True):
        self.router_sk = SecretKey.from_seed(bytes([9]) * 32).derive_synthetic()
        self.router_ph = standard_puzzle_hash(self.router_sk.public_key())
        op_sk = SecretKey.from_seed(bytes([7]) * 32).derive_synthetic()
        self.op = OperatorKey(op_sk)
        w = self.w = SimWorld(engine_ph=self.op.puzzle_hash, raw_config={"router_ph": self.router_ph})
        w.wallets[self.op.puzzle_hash] = types.SimpleNamespace(sk=op_sk, pk=op_sk.public_key(), puzzle_hash=self.op.puzzle_hash)
        w.sim.new_coin(self.op.puzzle_hash, 10**13)  # the operator's fee wallet
        w.sim.new_coin(self.router_ph, 10**12)  # the router's message coins: one per create in a block
        w.sim.new_coin(self.router_ph, 10**12 + 1)
        w.advance_to(w.h + 3)
        self.alice = w.sim.bls(0)
        self.src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, self.alice.puzzle_hash, 3)]})
        self.pools = {POOL: PoolInfo(frozenset({XCH, ASSET_A, ASSET_B}), 21)}
        self.chain = (chain_cls or SimChain)(w.sim)
        self.fleet = self.new_fleet(discover=discover)
        self.fleet.boot()

    def new_fleet(self, discover=True):
        return Fleet(self.chain, "testnet11", self.w.launcher_id, lambda cs: self.src, operator=self.op,
                     router_sk=self.router_sk, rules=RouterRules.permissive(), pools=lambda: self.pools, every=10,
                     discover=discover)

    @property
    def h(self):
        return self.w.sim.height()

    def creator(self):
        return self.w.sim.bls(0)

    def give_cat(self, who, asset, amount) -> "Cat":
        """A coin of `asset` at `who`, created by a real spend (its parent's spend is on chain, as
        the launch quote reads it)."""
        w = self.w
        clvm = w.clvm  # the Keys puzzles live in the SimWorld's allocator
        info = CatInfo(asset, None, w.keys.ph("minter"))
        grand = clvm.atom(b"mint" + asset + amount.to_bytes(8, "big") + who.puzzle_hash).tree_hash()
        parent = Coin(grand, info.puzzle_hash(), amount)
        coin = Coin(parent.coin_id(), info.puzzle_hash(), amount)
        w.sim.insert_coin(coin)
        minted = Cat(coin, LineageProof(grand, w.keys.ph("minter"), amount), info)
        clvm.spend_cats([CatSpend(minted, Spend(w.keys.puzzle("minter"),
                                                 to_program(clvm, [[51, who.puzzle_hash, amount, [who.puzzle_hash]]])))])
        w.sim.spend_coins(list(clvm.coin_spends()), [])
        return minted.child(who.puzzle_hash, amount)

    def request(self, creator, asset, **kw):
        body = {"creator": creator.puzzle_hash.hex(), "amount": 1_000_000, "length": 40, "kind": "LP_HOLD",
                "target": POOL.hex(), "claim_window": 160, "reward_asset": asset.hex()}
        body.update(kw)
        return body

    def sign(self, creator, spends):
        return KeySigner([creator.sk], EXTRA).sign(spends)

    # --- the creator's side of a launch, as the page builds it -----------------------------

    def launch_quote(self, creator, asset, amount=1_000_000, fleet=None, **kw):
        """Fund the creator, quote a launch: (quote, funding coin, seed Cat or None)."""
        w = self.w
        funding = w.sim.new_coin(creator.puzzle_hash, amount + 10**10)
        seed = None
        body = self.request(creator, asset, amount=amount, funding_coin=funding.coin_id().hex(), **kw)
        if asset != XCH:
            seed = self.give_cat(creator, asset, amount + 500)
            body["seed_coin"] = seed.coin.coin_id().hex()
        q = (fleet or self.fleet).launches.quote(body)
        return q, funding, seed

    def launch_bundle(self, creator, q, funding, seed, leave=None, drop=None):
        """The creator's spends for a launch quote. `drop` names a launch condition to leave out
        (refusal tests): "hint", "announcement", "reserve"."""
        clvm = Clvm()
        launch = q["launch"]
        amount = q["fields"]["amount"]
        fee = q["network_fee"]
        is_cat = seed is not None
        deposit = 0 if is_cat else amount
        msg = bytes.fromhex(q["conditions"]["message"]["message"])
        eve = bytes.fromhex(launch["eve_coin"])
        reserve_ph = bytes.fromhex(launch["reserve_puzzle_hash"])
        hint = bytes.fromhex(launch["hint"]) if drop != "hint" else b"\x00" * 32
        conds = [[66, 23, msg, eve], [51, LAUNCHER, 1, [hint]]]
        if not is_cat and drop != "reserve":
            conds.append([51, reserve_ph, 0])
        if drop != "announcement":
            conds.append([61, bytes.fromhex(launch["launcher_announcement"])])
        conds += [[51, creator.puzzle_hash, funding.amount - 1 - deposit - fee], [52, fee]]
        clvm.spend_standard_coin(funding, creator.pk, clvm.delegated_spend([to_program(clvm, c) for c in conds]))
        if is_cat:
            leave = amount if leave is None else leave
            inner = [[51, creator.puzzle_hash, seed.coin.amount - leave, [creator.puzzle_hash]]]
            if drop != "reserve":
                inner.insert(0, [51, reserve_ph, 0, [reserve_ph]])
            clvm.spend_cats([CatSpend(seed, clvm.standard_spend(creator.pk, clvm.delegated_spend(
                [to_program(clvm, c) for c in inner])))])
        spends = list(clvm.coin_spends())
        return {"quote": q["quote"], "spend_bundle": bundle_json(spends, self.sign(creator, spends))}

    def launch(self, creator, asset, fleet=None, **kw):
        q, funding, seed = self.launch_quote(creator, asset, fleet=fleet, **kw)
        out = (fleet or self.fleet).launches.create(self.launch_bundle(creator, q, funding, seed), wait=5, poll=0)
        return q, out

    # --- an ordinary CAT create on a hosted distributor -------------------------------------

    def cat_create_bundle(self, creator, q, asset, cat=None):
        clvm = Clvm()
        amount, fee = q["fields"]["amount"], q["network_fee"]
        msg = bytes.fromhex(q["conditions"]["message"]["message"])
        coin = self.w.sim.new_coin(creator.puzzle_hash, fee + 1)
        clvm.spend_standard_coin(coin, creator.pk, clvm.delegated_spend(
            [to_program(clvm, [66, 23, msg, bytes.fromhex(q["singleton"]["coin_id"])]),
             to_program(clvm, [51, creator.puzzle_hash, 1]), to_program(clvm, [52, fee])]))
        cat = cat or self.give_cat(creator, asset, amount + 7)
        clvm.spend_cats([CatSpend(cat, clvm.standard_spend(creator.pk, clvm.delegated_spend(
            [to_program(clvm, [51, creator.puzzle_hash, 7, [creator.puzzle_hash]])])))])
        spends = list(clvm.coin_spends())
        return {"quote": q["quote"], "spend_bundle": bundle_json(spends, self.sign(creator, spends))}

    def unspent(self, coin_id):
        s = self.w.sim.coin_state(coin_id)
        return s is not None and s.spent_height is None


def raw_launch(fx, router_ph=None, engine_ph=None, asset=XCH, finalizer=None, config=None, hint=True, state=None):
    """A distributor launched outside the fleet, hinted for this host: what anyone can put on
    chain. Its Config is the pin's template with `router_ph` / `config` overrides; `state`, when
    given, maps the genesis State's value to the one the eve is launched with instead."""
    w, pin = fx.w, fx.fleet.pin
    k = w.keys
    fund = w.sim.new_coin(k.ph("deployer"), 1)
    lid = Coin(fund.coin_id(), LAUNCHER, 1).coin_id()
    cfg = pin.config_for(lid, asset)
    for name, value in dict(config or {}, **({"router_ph": router_ph} if router_ph else {})).items():
        object.__setattr__(cfg, name, value)
    d = Distributor(Puzzles(Clvm()), cfg, engine_ph or pin.operator_ph, finalizer=finalizer)
    if state is not None:
        d.state = to_program(d.clvm, state(ChainState.genesis(engine_ph or pin.operator_ph).to_value()))
    conditions = d.funding_conditions(fund)
    if hint:
        conditions[0].append([pin.hint])
    seed_spends = []
    if asset != XCH:  # a CAT reserve is born from a seed coin of the CAT (an anyone-can-spend owner here)
        seed = fx.give_cat(types.SimpleNamespace(puzzle_hash=k.ph("deployer")), asset, 1)
        d.seed_reserve(seed)
        dc = d.clvm
        dc.spend_cats([CatSpend(seed, Spend(dc.deserialize(k.puzzle("deployer").serialize()), to_program(
            dc, [[51, d.reserve_ph, 0, [d.reserve_ph]], [51, k.ph("deployer"), 1]])))])
        seed_spends = list(dc.coin_spends())
    spends = [CoinSpend(fund, k.puzzle("deployer").serialize(), to_program(d.clvm, conditions).serialize()),
              d.launcher_spend(fund), *seed_spends]
    w.sim.spend_coins(spends, [])
    prepared = d.prepare([d.init(w.h)])
    w.sim.spend_coins(prepared.spends, [])
    d.apply(prepared)
    return lid


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestBuildPin(unittest.TestCase):
    """The pin check itself, on resynced distributors."""

    def setUp(self):
        self.fx = FleetWorld()
        self.pin = self.fx.fleet.pin
        self.got = resync(self.fx.w.sim, Puzzles(Clvm()), self.fx.w.launcher_id)

    def test_the_primary_is_the_pin(self):
        self.pin.check(self.got, reward_asset=XCH, pins=pins_fingerprint())
        self.assertEqual(self.pin.router_ph, self.fx.router_ph)
        self.assertEqual(self.pin.operator_ph, self.fx.op.puzzle_hash)

    def refused(self, why, pin=None, got=None, **kw):
        with self.assertRaises(NotPinned) as e:
            (pin or self.pin).check(got or self.got, **kw)
        self.assertIn(why, str(e.exception))

    def test_wrong_router_operator_asset_config_and_build_are_refused(self):
        self.refused("wrong router", dataclasses.replace(self.pin, router_ph=b"\x01" * 32))
        self.refused("wrong operator", dataclasses.replace(self.pin, operator_ph=b"\x02" * 32))
        self.refused("wrong asset", reward_asset=ASSET_A)
        self.refused("wrong config: guardian_ph", dataclasses.replace(self.pin, guardian_ph=b"\x03" * 32))
        self.refused("wrong config: dev_fee_ph", dataclasses.replace(self.pin, dev_fee_ph=b"\x04" * 32))
        self.refused("wrong config: fee_bps", dataclasses.replace(self.pin, fee_bps=301))
        self.refused("wrong config: dispute_bond", dataclasses.replace(self.pin, dispute_bond=1))
        self.refused("wrong build", pins="0" * 64)
        upstream = raw_launch(self.fx, finalizer=FINALIZER_UPSTREAM)
        self.refused("wrong build", got=resync(self.fx.w.sim, Puzzles(Clvm()), upstream))

    def test_a_pending_rotation_away_is_refused(self):
        got = self.got
        state = got.distributor.chain_state
        rotated = dataclasses.replace(state, rotation=types.SimpleNamespace(engine_ph=b"" * 32, effective_at=0))
        fake = types.SimpleNamespace(distributor=types.SimpleNamespace(finalizer_kind=got.distributor.finalizer_kind,
                                                                         chain_state=rotated), config=got.config)
        self.refused("rotation", got=fake)

    def test_the_canonical_order(self):
        a, b, c = b"\x0a" * 32, b"\x0b" * 32, b"\x0c" * 32
        self.assertEqual(canonical_order([(b, 7), (c, 5), (a, 7)]), [c, a, b])


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.fx = FleetWorld()

    def test_discovery_hosts_only_the_pinned_build(self):
        fx = self.fx
        good = raw_launch(fx)
        wrong_router = raw_launch(fx, router_ph=fx.w.keys.ph("router"))
        wrong_operator = raw_launch(fx, engine_ph=fx.w.keys.ph("engine"))
        wrong_build = raw_launch(fx, finalizer=FINALIZER_UPSTREAM)
        wrong_guardian = raw_launch(fx, config={"guardian_ph": fx.w.keys.ph("someone")})
        unhinted = raw_launch(fx, hint=False)
        added = fx.fleet.discover_new()
        self.assertEqual(added, [good])
        refused = fx.fleet.rejected
        self.assertIn("wrong router", refused[wrong_router])
        self.assertIn("wrong operator", refused[wrong_operator])
        self.assertIn("wrong build", refused[wrong_build])
        self.assertIn("wrong config: guardian_ph", refused[wrong_guardian])
        self.assertNotIn(unhinted, fx.fleet.hosted)
        self.assertNotIn(unhinted, refused)
        # The primary launched first, so it stays XCH's distributor; the newer one takes no creates.
        self.assertEqual(fx.fleet.canonical(XCH), fx.w.launcher_id)
        self.assertEqual(fx.fleet.gate(good), fx.w.launcher_id)


    def test_a_distributor_not_born_empty_is_refused_and_never_canonical(self):
        """F-B1 (2026-11-14 conformance review, part B): the launch chooses the eve's State, so a
        launch of the exact pinned build could start with a campaign row naming the launcher as
        creator and reclaim from honest creators' deposits. Only the empty genesis is hosted, byte
        for byte, and a refused launch never becomes an asset's distributor."""
        fx = self.fx
        good = raw_launch(fx)
        row = [b"\xfa" * 32, fx.w.keys.ph("thief"), 500_000, 1, 2, 1, 0, sha256tree([])]
        # The first (and only) launch for ASSET_A carries a row nobody created.
        seeded = raw_launch(fx, asset=ASSET_A, state=lambda v: v[:3] + [[row]] + v[4:])
        ninth = raw_launch(fx, state=lambda v: v + [b"\x01"])  # parses as genesis; bytes differ
        fx.fleet.discover_new()
        self.assertIn(good, fx.fleet.hosted)
        self.assertTrue(fx.fleet.get(good).build_pinned, fx.fleet.get(good).why_not)
        for bad in (seeded, ninth):
            self.assertNotIn(bad, fx.fleet.hosted)
            self.assertIn("not born empty", fx.fleet.rejected.get(bad, ""))
        self.assertIsNone(fx.fleet.canonical(ASSET_A), "a refused first launch must not become the asset's distributor")

    def test_a_configured_distributor_off_the_pin_is_hosted_but_never_canonical(self):
        """FLPCR_LAUNCHERS hosts any launcher it names, for reads and claims; only the build pin
        makes one an asset's distributor. A CAT whose only distributor is off the pin still launches."""
        fx = self.fx
        off = raw_launch(fx, router_ph=fx.w.keys.ph("router"), asset=ASSET_A)
        fleet = fx.new_fleet(discover=False)
        fleet.configured = [off]
        fleet.boot()
        self.assertIn(off, fleet.hosted)
        self.assertFalse(fleet.get(off).build_pinned)
        self.assertIn("wrong router", fleet.get(off).why_not)
        self.assertIsNone(fleet.canonical(ASSET_A))
        self.assertEqual(fleet.gate(off), off, "it takes no creates")
        q, _, _ = fx.launch_quote(fx.creator(), ASSET_A, fleet=fleet)  # not moved onto it
        self.assertNotEqual(q["launch"]["launcher_id"], off.hex())


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestFirstUseLaunch(unittest.TestCase):
    def setUp(self):
        self.fx = FleetWorld()

    def test_a_cat_launch_and_its_first_campaign_in_one_bundle(self):
        fx = self.fx
        creator = fx.creator()
        dev_before = sum(c.amount for c in fx.w.sim.unspent_coins(cat_puzzle_hash(ASSET_A, fx.fleet.pin.dev_fee_ph), False))
        router_before = sum(c.amount for c in fx.w.sim.unspent_coins(fx.router_ph, False))
        q, funding, seed = fx.launch_quote(creator, ASSET_A)
        launch = q["launch"]
        lid = bytes.fromhex(launch["launcher_id"])
        self.assertEqual(lid, Coin(funding.coin_id(), LAUNCHER, 1).coin_id())
        self.assertEqual(launch["reserve_parent"], seed.coin.coin_id().hex())
        self.assertEqual(q["singleton"]["coin_id"], launch["eve_coin"])
        self.assertEqual(q["fields"]["distributor"], launch["launcher_id"])
        out = fx.fleet.launches.create(fx.launch_bundle(creator, q, funding, seed), wait=5, poll=0)
        self.assertEqual(out["distributor"], lid.hex())
        hosted = fx.fleet.get(lid)
        self.assertTrue(hosted.build_pinned, hosted.why_not)
        self.assertEqual(fx.fleet.canonical(ASSET_A), lid)
        st = hosted.service.status()
        self.assertEqual([c["id"] for c in st["campaigns"]], [q["campaign"]])
        self.assertEqual(st["reserve"], q["budget"])
        self.assertEqual(st["reward_asset"], ASSET_A.hex())
        dev_after = sum(c.amount for c in fx.w.sim.unspent_coins(cat_puzzle_hash(ASSET_A, fx.fleet.pin.dev_fee_ph), False))
        self.assertEqual(dev_after - dev_before, q["fee"])
        self.assertEqual(sum(c.amount for c in fx.w.sim.unspent_coins(fx.router_ph, False)), router_before,
                         "the router pays nothing")
        # The creator's change: the seed's 500 back, and the XCH less the launcher's mojo and the fee.
        self.assertEqual(sum(c.amount for c in fx.w.sim.unspent_coins(cat_puzzle_hash(ASSET_A, creator.puzzle_hash), False)), 500)
        self.assertEqual(sum(c.amount for c in fx.w.sim.unspent_coins(creator.puzzle_hash, False)),
                         funding.amount - 1 - q["network_fee"])
        # A second campaign for the asset is an ordinary create there.
        with self.assertRaises(Moved) as e:
            fx.launch_quote(fx.creator(), ASSET_A)
        self.assertEqual(e.exception.distributor, lid)

    def test_an_xch_launch_is_moved_to_the_primary(self):
        with self.assertRaises(Moved) as e:
            self.fx.launch_quote(self.fx.creator(), XCH)
        self.assertEqual(e.exception.distributor, self.fx.w.launcher_id)

    def test_launch_bundles_missing_a_part_are_refused_before_push(self):
        fx = self.fx
        for drop, why in (("hint", "hint"), ("announcement", "announcement"), ("reserve", "empty reserve")):
            creator = fx.creator()
            q, funding, seed = fx.launch_quote(creator, ASSET_A)
            with self.assertRaises(ValueError) as e:
                fx.fleet.launches.create(fx.launch_bundle(creator, q, funding, seed, drop=drop), wait=5, poll=0)
            self.assertIn(why, str(e.exception))
            self.assertTrue(fx.unspent(funding.coin_id()) and fx.unspent(seed.coin.coin_id()))
        creator = fx.creator()
        q, funding, seed = fx.launch_quote(creator, ASSET_A)
        with self.assertRaises(ValueError) as e:
            fx.fleet.launches.create(fx.launch_bundle(creator, q, funding, seed, leave=999_999), wait=5, poll=0)
        self.assertIn("leaves 999,999", str(e.exception))
        self.assertIsNone(fx.fleet.canonical(ASSET_A))

    def test_the_funding_and_seed_coins_must_be_the_creators(self):
        fx = self.fx
        creator, other = fx.creator(), fx.creator()
        theirs = fx.w.sim.new_coin(other.puzzle_hash, 10**10)
        with self.assertRaises(ValueError) as e:
            fx.fleet.launches.quote(fx.request(creator, ASSET_A, funding_coin=theirs.coin_id().hex()))
        self.assertIn("creator's address", str(e.exception))
        mine = fx.w.sim.new_coin(creator.puzzle_hash, 10**10)
        their_cat = fx.give_cat(other, ASSET_A, 10**6)
        with self.assertRaises(ValueError) as e:
            fx.fleet.launches.quote(fx.request(creator, ASSET_A, funding_coin=mine.coin_id().hex(),
                                               seed_coin=their_cat.coin.coin_id().hex()))
        self.assertIn("reward CAT at the creator's address", str(e.exception))


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestLaunchRace(unittest.TestCase):
    def setUp(self):
        self.fx = FleetWorld()

    def test_the_second_launch_is_moved_onto_the_first_with_its_coins_untouched(self):
        fx = self.fx
        a, b = fx.creator(), fx.creator()
        qa, fa, sa = fx.launch_quote(a, ASSET_A)
        qb, fb, sb = fx.launch_quote(b, ASSET_A)  # both quoted while no distributor exists
        fx.fleet.launches.create(fx.launch_bundle(a, qa, fa, sa), wait=5, poll=0)
        winner = bytes.fromhex(qa["launch"]["launcher_id"])
        with self.assertRaises(Moved) as e:
            fx.fleet.launches.create(fx.launch_bundle(b, qb, fb, sb), wait=5, poll=0)
        self.assertEqual(e.exception.distributor, winner)
        self.assertTrue(fx.unspent(fb.coin_id()) and fx.unspent(sb.coin.coin_id()), "the loser's coins are untouched")
        self.assertNotIn(bytes.fromhex(qb["launch"]["launcher_id"]), fx.fleet.hosted)
        # Retried onto the winner as an ordinary create: both campaigns live on one distributor.
        intake = fx.fleet.for_asset(ASSET_A).intake
        q = intake.quote(fx.request(b, ASSET_A))
        self.assertEqual(q["fields"]["distributor"], winner.hex())
        intake.create(fx.cat_create_bundle(b, q, ASSET_A), wait=5, poll=0)
        ids = {c["id"] for c in fx.fleet.get(winner).service.status()["campaigns"]}
        self.assertEqual(ids, {qa["campaign"], q["campaign"]})

    def test_a_launch_waits_while_another_for_its_asset_is_confirming(self):
        fx = self.fx
        a = fx.creator()
        q, f, s = fx.launch_quote(a, ASSET_A)
        fx.fleet.launches.pushing[ASSET_A] = "someone else"
        with self.assertRaises(LeaseBusy):
            fx.fleet.launches.create(fx.launch_bundle(a, q, f, s), wait=5, poll=0)
        self.assertTrue(fx.unspent(f.coin_id()))
        del fx.fleet.launches.pushing[ASSET_A]
        fx.fleet.launches.create(fx.launch_bundle(a, q, f, s), wait=5, poll=0)
        self.assertIsNotNone(fx.fleet.canonical(ASSET_A))

    def test_two_hosts_launching_at_once_both_serve_and_the_first_is_canonical(self):
        """Out of band (a second host with the same keys, no discovery): both launches confirm.
        Both distributors are hosted and posted for; only the first takes creates."""
        fx = self.fx
        other = fx.new_fleet(discover=False)
        other.boot()
        qa, _ = fx.launch(fx.creator(), ASSET_A)
        qb, _ = fx.launch(fx.creator(), ASSET_A, fleet=other)
        first, second = bytes.fromhex(qa["launch"]["launcher_id"]), bytes.fromhex(qb["launch"]["launcher_id"])
        self.assertEqual(fx.fleet.discover_new(), [second])
        self.assertTrue(fx.fleet.get(second).build_pinned)
        self.assertEqual(fx.fleet.canonical(ASSET_A), first)
        self.assertEqual(fx.fleet.gate(second), first)
        with self.assertRaises(Moved) as e:
            fx.fleet.get(second).intake.quote(fx.request(fx.creator(), ASSET_A))
        self.assertEqual(e.exception.distributor, first)
        # The late one's campaign is still scored and paid: the fleet posts its roots.
        fx.w.advance_to(qb["fields"]["start"] + 12)
        out = fx.fleet.step(discover=False, log=lambda m: None)
        self.assertEqual(out[second.hex()]["posted"]["root_id"], 1)
        self.assertEqual(out[first.hex()]["posted"]["root_id"], 1)


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestSamePool(unittest.TestCase):
    """Owner (2026-11-14): several campaigns on one pool at once, by different creators, unless
    they land in the same block on the same distributor. No per-pool uniqueness."""

    def test_two_creators_one_pool_one_cat_back_to_back(self):
        fx = FleetWorld()
        fx.launch(fx.creator(), ASSET_A)
        intake = fx.fleet.for_asset(ASSET_A).intake
        a, b = fx.creator(), fx.creator()
        cat_a, cat_b = fx.give_cat(a, ASSET_A, 1_000_007), fx.give_cat(b, ASSET_A, 1_000_007)  # minted ahead
        qa = intake.quote(fx.request(a, ASSET_A))
        qb = intake.quote(fx.request(b, ASSET_A))
        self.assertEqual(qa["singleton"]["coin_id"], qb["singleton"]["coin_id"])
        at_a = intake.create(fx.cat_create_bundle(a, qa, ASSET_A, cat_a), wait=5, poll=0)["height"]
        with self.assertRaises(Requote):  # same coin: the second lands next block, re-quoted
            intake.create(fx.cat_create_bundle(b, qb, ASSET_A, cat_b), wait=5, poll=0)
        qb = intake.quote(fx.request(b, ASSET_A))
        at_b = intake.create(fx.cat_create_bundle(b, qb, ASSET_A, cat_b), wait=5, poll=0)["height"]
        self.assertEqual(at_b, at_a + 1)
        rows = fx.fleet.for_asset(ASSET_A).service.status()["campaigns"]
        live = [r for r in rows if r["target"] == POOL.hex() and r["creator"] in (a.puzzle_hash.hex(), b.puzzle_hash.hex())]
        self.assertEqual(len(live), 2)

    def test_two_cats_one_pool_one_block(self):
        fx = FleetWorld(chain_cls=HeldChain)
        a, b = fx.creator(), fx.creator()
        qa, fa, sa = fx.launch_quote(a, ASSET_A)
        qb, fb, sb = fx.launch_quote(b, ASSET_B)
        bundles = [fx.launch_bundle(a, qa, fa, sa), fx.launch_bundle(b, qb, fb, sb)]
        fx.chain.hold = True
        results, errors = [], []

        def run(body):
            try:
                results.append(fx.fleet.launches.create(body, wait=20, poll=0.05))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=run, args=(x,)) for x in bundles]
        for t in threads:
            t.start()
        deadline = time.time() + 20
        while len(fx.chain.held) < 2 and time.time() < deadline and not errors:
            time.sleep(0.02)
        fx.chain.hold = False
        fx.chain.flush()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        heights = {r["height"] for r in results}
        self.assertEqual(len(heights), 1, "both launches landed in one block")
        la, lb = fx.fleet.canonical(ASSET_A), fx.fleet.canonical(ASSET_B)
        self.assertNotEqual(la, lb)
        for lid, q in ((la, qa), (lb, qb)):
            rows = fx.fleet.get(lid).service.status()["campaigns"]
            self.assertEqual([(r["id"], r["target"]) for r in rows], [(q["campaign"], POOL.hex())])


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestFleetLoop(unittest.TestCase):
    def test_roots_claims_and_a_cold_restart_for_every_distributor(self):
        fx = FleetWorld()
        qa, _ = fx.launch(fx.creator(), ASSET_A)
        qb, _ = fx.launch(fx.creator(), ASSET_B)
        la, lb = fx.fleet.canonical(ASSET_A), fx.fleet.canonical(ASSET_B)
        fx.w.advance_to(max(qa["fields"]["end"], qb["fields"]["end"]) + 1)
        out = fx.fleet.step(discover=False, log=lambda m: None)
        for lid in (la, lb):
            self.assertEqual(out[lid.hex()]["posted"]["root_id"], 1)
        hosted = fx.fleet.get(la)
        fx.w.advance_to(hosted.service.status()["pending"]["activates_at"] + 1)
        leaf = next(x for x in hosted.service.claimable(fx.alice.puzzle_hash)["claims"] if x["campaign"] == qa["campaign"])
        self.assertGreater(leaf["claimable"], 0)
        funder = fx.w.sim.bls(0)
        coin = fx.w.sim.new_coin(funder.puzzle_hash, 10**11)
        clvm = Clvm()
        clvm.spend_standard_coin(coin, funder.pk, clvm.delegated_spend(
            [to_program(clvm, [51, funder.puzzle_hash, coin.amount - FEE_PER_CLAIM])]))
        spends = list(clvm.coin_spends())
        body = {"claims": [{"campaign": qa["campaign"], "puzzle_hash": fx.alice.puzzle_hash.hex(),
                            "public_key": fx.alice.pk.to_bytes().hex()}],
                "signature": fx.alice.sk.sign(bytes.fromhex(leaf["leaf"])).to_bytes().hex(),
                "spend_bundle": bundle_json(spends, KeySigner([funder.sk], EXTRA).sign(spends))}
        hosted.sequencer.submit(body)
        fx.fleet.step(discover=False, log=lambda m: None)
        paid = sum(c.amount for c in fx.w.sim.unspent_coins(cat_puzzle_hash(ASSET_A, fx.alice.puzzle_hash), False))
        self.assertEqual(paid, leaf["claimable"])
        # A fresh service, from the chain alone, hosts all three with the same canonical choices.
        cold = fx.new_fleet()
        cold.boot()
        self.assertEqual(set(cold.hosted), {fx.w.launcher_id, la, lb})
        self.assertEqual((cold.canonical(ASSET_A), cold.canonical(ASSET_B)), (la, lb))
        self.assertTrue(all(h.build_pinned for h in cold.hosted.values()))

    def test_http_routes(self):
        fx = FleetWorld()
        qa, _ = fx.launch(fx.creator(), ASSET_A)
        la = fx.fleet.canonical(ASSET_A)
        server = serve(None, 0, fleet=fx.fleet, post_limit=PostLimit(1000))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def get(path):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        def post(path, body):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        try:
            code, fleet = get("/api/v1/fleet")
            self.assertEqual(code, 200)
            rows = {r["launcher_id"]: r for r in fleet["distributors"]}
            self.assertTrue(rows[la.hex()]["canonical"] and rows[la.hex()]["creates"])
            self.assertEqual(fleet["build"]["router_ph"], fx.router_ph.hex())
            self.assertEqual(get("/api/v1/status")[1]["launcher_id"], fx.w.launcher_id.hex())
            self.assertEqual(get(f"/d/{la.hex()}/api/v1/status")[1]["launcher_id"], la.hex())
            self.assertEqual(get(f"/a/{ASSET_A.hex()}/api/v1/status")[1]["launcher_id"], la.hex())
            self.assertEqual(get(f"/a/{ASSET_B.hex()}/api/v1/status")[0], 404)
            terms = get(f"/api/v1/launch/terms/{ASSET_B.hex()}")[1]
            self.assertEqual((terms["reward_asset"], terms["distributor"]), (ASSET_B.hex(), None))
            self.assertEqual(get(f"/api/v1/launch/terms/{ASSET_A.hex()}")[1]["distributor"], la.hex())
            code, body = post("/api/v1/launch/quote", fx.request(fx.creator(), ASSET_A,
                                                                funding_coin=fx.w.sim.new_coin(b"\x00" * 32, 1).coin_id().hex()))
            self.assertEqual((code, body["requote"], body["distributor"]), (409, True, la.hex()))
        finally:
            server.shutdown()



@unittest.skipUnless(HAVE_CHAIN, "needs chia_wallet_sdk and the compiled build")
class TestTemplateMode(unittest.TestCase):
    """Mainnet hosting (owner, 2026-11-25: "anyone should be able to launch a campaign"): no primary
    distributor and no operator-paid launch. The build pin comes from the host's settings
    (guardian, admin, dev fee, its two keys, production Config defaults) and the first creator's
    campaign launches the first distributor, which the host then finds by its hint and serves."""

    GUARDIAN, ADMIN, DEV = b"\x6a" * 32, b"\x6b" * 32, b"\x6c" * 32

    def template(self, fx, **kw):
        from flpcr.build_pin import BuildPin
        roles = dict(operator_ph=fx.op.puzzle_hash, router_ph=fx.router_ph, guardian_ph=self.GUARDIAN,
                     admin_ph=self.ADMIN, dev_fee_ph=self.DEV, pins=fx.fleet.pins)
        roles.update(kw)
        return BuildPin.template("testnet11", **roles)

    def fleet(self, fx, template=None, discover=True):
        return Fleet(fx.chain, "testnet11", None, lambda cs: fx.src, operator=fx.op, router_sk=fx.router_sk,
                     rules=RouterRules.permissive(), pools=lambda: fx.pools, every=10, discover=discover,
                     template=template or self.template(fx))

    def test_the_template_is_the_production_config(self):
        from flpcr.distributor import Config, DEFAULT_DISPUTE_BOND
        fx = FleetWorld(discover=False)
        pin = self.template(fx)
        d = Config(bytes(32), bytes(32), self.DEV, self.ADMIN, self.GUARDIAN, fx.router_ph)
        self.assertEqual((pin.dispute_blocks, pin.rotate_delay, pin.dispute_bond, pin.fee_bps, pin.max_campaigns),
                         (d.dispute_blocks, d.rotate_delay, DEFAULT_DISPUTE_BOND, d.fee_bps, d.max_campaigns))
        self.assertEqual((pin.guardian_ph, pin.admin_ph, pin.dev_fee_ph), (self.GUARDIAN, self.ADMIN, self.DEV))
        with self.assertRaisesRegex(ValueError, "not template fields"):
            self.template(fx, reward_asset=bytes(32))
        with self.assertRaisesRegex(ValueError, "rotate_delay must exceed"):
            self.template(fx, rotate_delay=1)

    def test_boot_empty_then_the_first_creator_launches_xch(self):
        fx = FleetWorld(discover=False)
        fleet = self.fleet(fx)
        fleet.boot()
        self.assertTrue(fleet.booted)
        self.assertEqual(fleet.hosted, {}, "the primary-mode world's distributor does not pass this pin")
        self.assertIsNone(fleet.home())
        self.assertTrue(fleet.describe()["template"])
        self.assertEqual(fleet.terms(XCH)["reward_asset"], XCH.hex())
        q, out = fx.launch(fx.creator(), XCH, fleet=fleet, length=80, claim_window=300)
        lid = bytes.fromhex(out["distributor"])
        self.assertEqual(fleet.canonical(XCH), lid)
        self.assertIs(fleet.home(), fleet.get(lid))
        h = fleet.get(lid)
        self.assertTrue(h.build_pinned, h.why_not)
        cfg = h.service.d.cfg
        self.assertEqual((cfg.guardian_ph, cfg.admin_ph, cfg.dev_fee_ph, cfg.router_ph),
                         (self.GUARDIAN, self.ADMIN, self.DEV, fx.router_ph))
        self.assertEqual(len(h.service.s.campaigns), 1)
        self.assertEqual(fx.w.sim.coin_state(bytes.fromhex(q["launch"]["funding_coin"])).spent_height is not None, True)
        fleet.step()  # the operator loop runs on the launched distributor
        # A cold restart finds it from chain alone, by its hint.
        again = self.fleet(fx)
        again.boot()
        self.assertEqual(again.canonical(XCH), lid)
        self.assertTrue(again.get(lid).build_pinned)

    def test_a_template_must_name_this_hosts_keys(self):
        fx = FleetWorld(discover=False)
        with self.assertRaisesRegex(ValueError, "router is not this host's"):
            self.fleet(fx, self.template(fx, router_ph=b"\x01" * 32)).boot()
        with self.assertRaisesRegex(ValueError, "operator is not this host's"):
            self.fleet(fx, self.template(fx, operator_ph=b"\x02" * 32)).boot()
        with self.assertRaisesRegex(ValueError, "another compiled build"):
            self.fleet(fx, self.template(fx, pins="0" * 64)).boot()
        with self.assertRaisesRegex(ValueError, "no primary distributor and no template"):
            Fleet(fx.chain, "testnet11", None, lambda cs: fx.src).boot()

    def test_a_distributor_under_another_guardian_is_refused(self):
        """Discovery only hosts what the template pins: a launch hinted for this host under any other
        guardian (or admin) never takes creates."""
        fx = FleetWorld(discover=False)
        other = self.fleet(fx, self.template(fx, guardian_ph=b"\x99" * 32))
        other.boot()
        _, out = fx.launch(fx.creator(), XCH, fleet=other, length=80, claim_window=300)
        stray = bytes.fromhex(out["distributor"])
        fleet = self.fleet(fx)
        fleet.boot()
        self.assertNotIn(stray, fleet.hosted)
        self.assertIn("guardian_ph", fleet.rejected.get(stray, ""))
        self.assertIsNone(fleet.canonical(XCH))

    def test_template_from_the_hosts_env(self):
        """main.template_from_env: the guardian from FLPCR_GUARDIAN_ADDRESS, the admin and dev fee
        from flpcr.networks (mainnet: the dev fee address only from its env), both keys required."""
        from chia_wallet_sdk import Address
        from flpcr.service.main import template_from_env
        fx = FleetWorld(discover=False)
        xch = lambda b: Address(b, "xch").encode()  # noqa: E731 -- made-up addresses, never the real ones
        env = {"FLPCR_GUARDIAN_ADDRESS": xch(self.GUARDIAN), "VITE_CFMM_POOL_DEV_FEE_ADDRESS_MAINNET": xch(self.DEV)}
        pin = template_from_env("mainnet", env, fx.op, fx.router_sk)
        self.assertEqual((pin.network, pin.guardian_ph, pin.admin_ph, pin.dev_fee_ph, pin.router_ph, pin.operator_ph),
                         ("mainnet", self.GUARDIAN, self.DEV, self.DEV, fx.router_ph, fx.op.puzzle_hash))
        pin = template_from_env("mainnet", {**env, "FLPCR_ADMIN_ADDRESS": xch(self.ADMIN)}, fx.op, fx.router_sk)
        self.assertEqual(pin.admin_ph, self.ADMIN)
        with self.assertRaisesRegex(ValueError, "FLPCR_GUARDIAN_ADDRESS"):
            template_from_env("mainnet", {k: v for k, v in env.items() if k != "FLPCR_GUARDIAN_ADDRESS"}, fx.op, fx.router_sk)
        with self.assertRaisesRegex(ValueError, "needs FLPCR_OPERATOR_SK and FLPCR_ROUTER_SK"):
            template_from_env("mainnet", env, None, fx.router_sk)
        with self.assertRaises(ValueError):  # the dev fee address is required on mainnet
            template_from_env("mainnet", {"FLPCR_GUARDIAN_ADDRESS": xch(self.GUARDIAN)}, fx.op, fx.router_sk)
        with self.assertRaises(ValueError):  # a testnet address on mainnet
            template_from_env("mainnet", {**env, "FLPCR_GUARDIAN_ADDRESS": Address(self.GUARDIAN, "txch").encode()},
                              fx.op, fx.router_sk)

    def test_http_before_and_after_the_first_launch(self):
        fx = FleetWorld(discover=False)
        fleet = self.fleet(fx)
        fleet.boot()
        server = serve(None, 0, fleet=fleet, post_limit=PostLimit(1000))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def get(path):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        try:
            code, health = get("/health")
            self.assertEqual((code, health["distributors"]), (200, 0))
            self.assertEqual(get("/api/v1/status")[0], 404)
            code, terms = get(f"/api/v1/launch/terms/{XCH.hex()}")
            self.assertEqual((code, terms["distributor"]), (200, None))
            code, desc = get("/api/v1/fleet")
            self.assertEqual((code, desc["primary"], desc["template"]), (200, None, True))
            self.assertEqual(desc["build"]["guardian_ph"], self.GUARDIAN.hex())
            _, out = fx.launch(fx.creator(), XCH, fleet=fleet, length=80, claim_window=300)
            code, status = get("/api/v1/status")
            self.assertEqual((code, status["launcher_id"]), (200, out["distributor"]))
            self.assertEqual(get(f"/api/v1/launch/terms/{XCH.hex()}")[1]["distributor"], out["distributor"])
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
