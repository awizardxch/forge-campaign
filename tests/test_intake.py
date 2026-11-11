"""The create intake on the simulator: a quote, the creator's signed spend, the router's
co-signature and one push; refusals at quote time and before push; the lease that keeps
the operator and the claim sequencer off the singleton while a create is quoted."""

import json
import threading
import types
import unittest
import urllib.error
import urllib.request

try:
    from chia_wallet_sdk import Address, Cat, CatInfo, CatSpend, Clvm, Coin, LineageProof, SecretKey, Signature, standard_puzzle_hash

    from flpcr.chain.nfts import collection_id
    from flpcr.chain.signer import KeySigner
    from flpcr.chain.sim import SimWorld
    from flpcr.chain.values import to_program
    from flpcr.service.chain import SimChain, bundle_json
    from flpcr.service.core import CampaignService, OperatorKey
    from flpcr.service.http import PostLimit, serve
    from flpcr.service.intake import CampaignIntake, PoolInfo, Requote
    from flpcr.service.lease import LeaseBusy, SingletonLease
    from flpcr.service.sequencer import ClaimSequencer, Pending

    from flpcr.service.core import MIN_FEE
    HAVE_CHAIN = True
except (ImportError, FileNotFoundError):  # pragma: no cover
    HAVE_CHAIN = False

from flpcr import FixtureSource, HolderEvent
from flpcr.router import XCH, RouterRules

POOL, OLD_POOL, LP = b"\xb0" * 32, b"\xb1" * 32, b"\x1b" * 32
ASSET = b"\xca" * 32
EXTRA = bytes.fromhex("37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class IntakeWorld:
    """A distributor whose router is a real key held by the intake, and a booted service."""

    def __init__(self, reward_asset=bytes(32), rules=RouterRules.permissive(), router=True):
        self.router_sk = SecretKey.from_seed(bytes([9]) * 32).derive_synthetic()
        router_ph = standard_puzzle_hash(self.router_sk.public_key())
        op_sk = SecretKey.from_seed(bytes([7]) * 32).derive_synthetic()
        self.op = OperatorKey(op_sk)
        w = self.w = SimWorld(engine_ph=self.op.puzzle_hash, reward_asset=reward_asset, raw_config={"router_ph": router_ph})
        w.wallets[self.op.puzzle_hash] = types.SimpleNamespace(sk=op_sk, pk=op_sk.public_key(), puzzle_hash=self.op.puzzle_hash)
        w.sim.new_coin(self.op.puzzle_hash, 10**13)
        w.sim.new_coin(router_ph, 10**12)  # the router's fee and message coin
        w.advance_to(w.h + 3)
        self.alice = w.keys.ph("alice")
        self.src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, self.alice, 3)]})
        self.svc = CampaignService(SimChain(w.sim), w.launcher_id, "testnet11", lambda cs: self.src, operator=self.op, every=10)
        self.clock = Clock()
        self.svc.lease = SingletonLease(self.clock)
        self.svc.boot()
        pools = {POOL: PoolInfo(frozenset({XCH, ASSET}), 17), OLD_POOL: PoolInfo(frozenset({XCH}), 16)}
        self.intake = CampaignIntake(self.svc, lambda: pools, rules, router_sk=self.router_sk if router else None)
        self.creator = w.sim.bls(0)

    def request(self, **kw):
        body = {"creator": self.creator.puzzle_hash.hex(), "amount": 1_000_000, "length": 40, "kind": "LP_HOLD",
                "target": POOL.hex(), "claim_window": 160, "excluded": [(b"\x99" * 32).hex()]}
        body.update(kw)
        return body

    def sign(self, spends, extra_sks=()):
        return KeySigner([self.creator.sk, *extra_sks], EXTRA).sign(spends)

    def xch_bundle(self, quote, leave=None, receiver=None, extra_sig=False, fee=None):
        """The creator's XCH coin: sends "k" + id to the quoted coin, leaves `leave` unspent for the
        deposit and reserves `fee` (the quote's network_fee) for the network."""
        w = self.w
        leave = quote["fields"]["amount"] if leave is None else leave
        fee = quote["network_fee"] if fee is None else fee
        coin = w.sim.new_coin(self.creator.puzzle_hash, leave + fee + 5_000_000)
        msg = bytes.fromhex(quote["conditions"]["message"]["message"])
        to = bytes.fromhex(receiver or quote["singleton"]["coin_id"])
        clvm = Clvm()
        conds = [[66, 23, msg, to], [51, self.creator.puzzle_hash, coin.amount - leave - fee], [52, fee]]
        clvm.spend_standard_coin(coin, self.creator.pk, clvm.delegated_spend([to_program(clvm, c) for c in conds]))
        spends = list(clvm.coin_spends())
        sig = self.sign(spends)
        if extra_sig:
            sig = Signature.aggregate([sig, self.creator.sk.sign(b"something else")])
        return bundle_json(spends, sig)

    def cat_bundle(self, quote, leave=None):
        """The creator's consent from an XCH coin, and a CAT coin of the reward leaving `leave`."""
        w = self.w
        amount = quote["fields"]["amount"]
        leave = amount if leave is None else leave
        msg = bytes.fromhex(quote["conditions"]["message"]["message"])
        clvm = Clvm()
        fee = quote["network_fee"]
        coin = w.sim.new_coin(self.creator.puzzle_hash, fee + 1)
        clvm.spend_standard_coin(coin, self.creator.pk, clvm.delegated_spend(
            [to_program(clvm, [66, 23, msg, bytes.fromhex(quote["singleton"]["coin_id"])]),
             to_program(clvm, [51, self.creator.puzzle_hash, 1]), to_program(clvm, [52, fee])]))
        info = CatInfo(ASSET, None, self.creator.puzzle_hash)
        held = amount + 500
        grand = clvm.atom(b"creator-cat").tree_hash()
        parent = Coin(grand, info.puzzle_hash(), held)
        cat_coin = Coin(parent.coin_id(), info.puzzle_hash(), held)
        w.sim.insert_coin(cat_coin)
        cat = Cat(cat_coin, LineageProof(grand, self.creator.puzzle_hash, held), info)
        inner = clvm.standard_spend(self.creator.pk, clvm.delegated_spend(
            [to_program(clvm, [51, self.creator.puzzle_hash, held - leave, [self.creator.puzzle_hash]])]))
        clvm.spend_cats([CatSpend(cat, inner)])  # a ring of one: the intake rebuilds the ring with the reserve
        spends = list(clvm.coin_spends())
        return bundle_json(spends, self.sign(spends))


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestXchCreate(unittest.TestCase):
    def setUp(self):
        self.x = IntakeWorld()

    def test_quote_sign_create(self):
        x = self.x
        q = x.intake.quote(x.request())
        self.assertEqual(q["fee"], 30_000)
        self.assertEqual(q["budget"], 970_000)
        self.assertEqual(q["daily_emission"], 970_000 * 4608 // 40)
        self.assertEqual(q["reclaim_from"], q["fields"]["end"] + 160)
        self.assertEqual(q["singleton"]["coin_id"], x.svc.d.coin.coin_id().hex())
        self.assertEqual(q["conditions"]["message"]["message"], "6b" + q["campaign"])
        self.assertTrue(x.svc.lease.busy())
        dev_before = x.w.xch_balance("dev")
        router_before = sum(k.amount for k in x.w.sim.unspent_coins(x.svc.d.cfg.router_ph, False))
        self.assertGreaterEqual(q["network_fee"], MIN_FEE)
        out = x.intake.create({"quote": q["quote"], "spend_bundle": x.xch_bundle(q)})
        self.assertEqual(out["status"], "created")
        # The creator paid the network fee; the router's coin came back whole.
        self.assertEqual(out["network_fee"], q["network_fee"])
        self.assertEqual(sum(k.amount for k in x.w.sim.unspent_coins(x.svc.d.cfg.router_ph, False)), router_before)
        self.assertFalse(x.svc.lease.busy())
        row = next(c for c in x.svc.status()["campaigns"] if c["id"] == q["campaign"])
        self.assertEqual(row["budget"], 970_000)
        self.assertEqual(row["excluded"], [(b"\x99" * 32).hex()])
        self.assertEqual(x.w.xch_balance("dev") - dev_before, 30_000)
        self.assertEqual(x.svc.d.reserve_coin.amount, 970_000)
        page = x.intake.campaign(q["campaign"])
        self.assertEqual((page["fee"], page["daily_emission"], page["reclaim_from"]),
                         (30_000, q["daily_emission"], q["reclaim_from"]))
        # A fresh service rebuilt from chain finds the same campaign.
        again = CampaignService(SimChain(x.w.sim), x.w.launcher_id, "testnet11", lambda cs: x.src)
        again.boot()
        self.assertIn(bytes.fromhex(q["campaign"]), again.s.campaigns)

    def test_refused_quotes(self):
        x = self.x
        with self.assertRaisesRegex(ValueError, "V15 pool"):
            x.intake.quote(x.request(target=OLD_POOL.hex()))
        with self.assertRaisesRegex(ValueError, "not a registered Forge pool"):
            x.intake.quote(x.request(target=(b"\x42" * 32).hex()))
        with self.assertRaisesRegex(ValueError, "claim_window"):
            x.intake.quote(x.request(claim_window=10))
        with self.assertRaisesRegex(ValueError, "length"):
            x.intake.quote(x.request(length=5))
        with self.assertRaisesRegex(ValueError, "start must be at least"):
            x.intake.quote(x.request(start=x.w.h + 3))
        with self.assertRaisesRegex(ValueError, "at most 16"):
            x.intake.quote(x.request(excluded=[bytes([i]) * 32 for i in range(17)]))
        strict = IntakeWorld(rules=RouterRules.permissive(min_amount={XCH: 10**9}))
        with self.assertRaisesRegex(ValueError, "below the minimum"):
            strict.intake.quote(strict.request())
        self.assertFalse(x.svc.lease.busy())  # a refused quote takes no lease

    def test_nft_quotes(self):
        x = self.x
        did = b"\xdd" * 32
        q = x.intake.quote(x.request(kind="NFT_HOLD", target=None, collection={"did": did.hex(), "id": "wizards"}))
        self.assertEqual(q["fields"]["target"], collection_id(did, "wizards").hex())
        self.assertEqual(q["fields"]["collection"], {"did": did.hex(), "id": "wizards"})
        x.clock.t += 121
        with self.assertRaisesRegex(ValueError, "col1 target needs collection"):
            x.intake.quote(x.request(kind="NFT_HOLD", target=Address(collection_id(did, "w"), "col").encode()))
        by_did = x.intake.quote(x.request(kind="NFT_HOLD", target=did.hex()))
        self.assertEqual(by_did["fields"]["target"], did.hex())
        self.assertEqual(x.intake.create({"quote": by_did["quote"], "spend_bundle": x.xch_bundle(by_did)})["status"], "created")

    def test_bad_bundles_are_refused_before_push(self):
        x = self.x
        q = x.intake.quote(x.request())
        coin = x.svc.d.coin
        for bundle, why in ((x.xch_bundle(q, leave=999_999), "needs 1,000,000"),
                            (x.xch_bundle(q, fee=1_000), "reserve 1,000 mojos for the network fee"),
                            (x.xch_bundle(q, receiver=(b"\x01" * 32).hex()), "quoted singleton coin"),
                            (x.xch_bundle(q, extra_sig=True), "does not verify")):
            with self.assertRaisesRegex(ValueError, why):
                x.intake.create({"quote": q["quote"], "spend_bundle": bundle})
        self.assertEqual(x.svc.d.coin, coin)  # nothing pushed
        self.assertTrue(x.svc.lease.busy())  # the quote still holds; a good bundle still lands
        self.assertEqual(x.intake.create({"quote": q["quote"], "spend_bundle": x.xch_bundle(q)})["status"], "created")

    def test_a_lapsed_lease_needs_a_requote(self):
        x = self.x
        q = x.intake.quote(x.request())
        x.clock.t += 121
        self.assertFalse(x.svc.lease.busy())
        with self.assertRaisesRegex(Requote, "lapsed"):
            x.intake.create({"quote": q["quote"], "spend_bundle": x.xch_bundle(q)})
        q2 = x.intake.quote(x.request())
        self.assertEqual(q2["campaign"], q["campaign"])
        self.assertEqual(x.intake.create({"quote": q2["quote"], "spend_bundle": x.xch_bundle(q2)})["status"], "created")

    def test_a_moved_singleton_needs_a_requote(self):
        x = self.x
        q = x.intake.quote(x.request())
        x.w.d = x.svc.d
        c = x.w.campaign("other", 1_000_000, x.w.h + 40, x.w.h + 80, 1, POOL, claim_window=160)
        call = x.svc.d.create(x.w.h, c.creator, c.amount, c.start, c.end, int(c.kind), c.target, c.params.as_clvm(),
                              c.nonce, c.claim_window, router=x.svc.d.cfg.router_ph)
        # Another builder (not this service) spends the singleton: the router's message comes from its key.
        prepared = x.svc.d.prepare([call])
        spends = list(prepared.spends)
        clvm = Clvm()
        rc = sorted(x.w.sim.unspent_coins(x.svc.d.cfg.router_ph, False), key=lambda k: k.amount)[0]
        clvm.spend_standard_coin(rc, x.router_sk.public_key(), clvm.delegated_spend([
            to_program(clvm, [66, 23, b"c" + c.id, x.svc.d.coin.coin_id()]), to_program(clvm, [51, rc.puzzle_hash, rc.amount])]))
        spends += list(clvm.coin_spends()) + x.w.send(c.creator, b"k" + c.id) + x.w.fee_spend(0) + \
            [x.w.keys.spend(x.w.sim.new_coin(x.w.keys.ph("depositor"), c.amount), "depositor", [])]
        x.w.sim.spend_coins(spends, [x.router_sk])
        x.svc.boot()
        with self.assertRaisesRegex(Requote, "moved"):
            x.intake.create({"quote": q["quote"], "spend_bundle": x.xch_bundle(q)})
        self.assertFalse(x.svc.lease.busy())
        q2 = x.intake.quote(x.request())
        self.assertNotEqual(q2["singleton"]["coin_id"], q["singleton"]["coin_id"])
        self.assertEqual(x.intake.create({"quote": q2["quote"], "spend_bundle": x.xch_bundle(q2)})["status"], "created")

    def test_no_router_key_quotes_but_does_not_create(self):
        x = IntakeWorld(router=False)
        q = x.intake.quote(x.request())
        with self.assertRaisesRegex(ValueError, "does not hold the router key"):
            x.intake.create({"quote": q["quote"], "spend_bundle": x.xch_bundle(q)})

    def test_the_lease_keeps_other_writers_off(self):
        x = self.x
        q = x.intake.quote(x.request())
        x.intake.create({"quote": q["quote"], "spend_bundle": x.xch_bundle(q)})
        x.w.advance_to(q["fields"]["start"] + 12)
        x.intake.quote(x.request(nonce=1))  # a second creator's quote holds the singleton
        coin = x.svc.d.coin
        self.assertIsNone(x.svc.operator_step(wait=5, poll=0))
        seq = ClaimSequencer(x.svc)
        seq.queue["p"] = Pending("p", [], Signature.infinity(), [], 0)
        self.assertIsNone(seq.run_batch(wait=5, poll=0))
        self.assertEqual(seq.queue["p"].status, "queued")
        self.assertEqual(x.svc.d.coin, coin)
        x.clock.t += 121  # the quote lapses unused: the operator posts again
        del seq.queue["p"]
        self.assertEqual(x.svc.operator_step(wait=5, poll=0)["root_id"], 1)


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestDefaultRouterRules(unittest.TestCase):
    """Audit 2026-11-10, CS-04 and CS-05. The hosted service ran `RouterRules()` as it came:
    no amount floor, every kind, the puzzle's ~90-minute claim window. 32 dust campaigns filled
    the table, and a creator could pick the shortest window and reclaim what holders earned."""

    def setUp(self):
        self.x = IntakeWorld(rules=RouterRules())
        self.ok = dict(amount=10**11, claim_window=7 * 4608)

    def refused(self, why, **kw):
        with self.assertRaisesRegex(ValueError, why):
            self.x.intake.quote(self.x.request(**{**self.ok, **kw}))

    def test_what_the_defaults_sign_for(self):
        x = self.x
        q = x.intake.quote(x.request(**self.ok))
        self.assertEqual(q["fields"]["claim_window"], 7 * 4608)
        body = x.request(amount=10**11)
        del body["claim_window"]  # what the site sends: the window is left to the service
        self.assertEqual(x.intake.quote(body)["fields"]["claim_window"], 7 * 4608)

    def test_what_the_defaults_refuse(self):
        self.refused("below the minimum", amount=10**11 - 1)
        self.refused("claim window is under", claim_window=160)
        self.refused("does not sign for CAT_HOLD", kind="CAT_HOLD", target=ASSET.hex())
        self.refused("does not sign for NFT_HOLD", kind="NFT_HOLD", target=(b"\xdd" * 32).hex())
        self.refused("blocks ahead", start=self.x.w.h + 31 * 4608)

    def test_rules_from_env_keep_the_defaults_for_what_is_left_out(self):
        from flpcr.service.main import rules_from_env
        self.assertEqual(rules_from_env(""), RouterRules())
        r = rules_from_env('{"min_claim_window": 160, "kinds": ["LP_HOLD", "NFT_HOLD"]}')
        self.assertEqual((r.min_claim_window, len(r.kinds), r.min_amount), (160, 2, RouterRules().min_amount))


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestQuotesCannotHoldTheDistributor(unittest.TestCase):
    """Audit 2026-11-10, CS-01. A quote needs no key, coin or signature. Asking again every
    two minutes held the one lease forever: no root, no claim batch, no other creator."""

    def setUp(self):
        self.x = IntakeWorld()

    def test_quotes_do_not_block_each_other(self):
        x = self.x
        x.intake.quote(x.request(creator=(b"\xaa" * 32).hex(), nonce=5))  # anyone's, never signed
        q = x.intake.quote(x.request())
        self.assertEqual(x.intake.create({"quote": q["quote"], "spend_bundle": x.xch_bundle(q)})["status"], "created")

    def test_an_endless_stream_of_quotes_delays_the_operator_once(self):
        x = self.x
        q = x.intake.quote(x.request())
        x.intake.create({"quote": q["quote"], "spend_bundle": x.xch_bundle(q)})
        x.w.advance_to(q["fields"]["end"] + 1)
        self.assertIsNotNone(x.svc.due(x.svc.chain.peak()))
        posted = None
        for i in range(8):  # eight lease periods of someone's quotes, back to back
            x.intake.quote(x.request(creator=(b"\xaa" * 32).hex(), nonce=100 + i))
            posted = posted or x.svc.operator_step(wait=5, poll=0)
            x.clock.t += 100
        self.assertIsNotNone(posted)  # within MAX_DEFER, not never
        self.assertEqual(posted["root_id"], 1)

    def test_a_pinned_create_is_exclusive(self):
        lease = SingletonLease(Clock())
        lease.take("a", b"\x01" * 32)
        lease.take("b", b"\x01" * 32)
        lease.pin("a")
        self.assertTrue(lease.pinned())
        with self.assertRaises(LeaseBusy):
            lease.pin("b")
        with self.assertRaises(LeaseBusy):
            lease.take("c", b"\x01" * 32)
        lease.release("a")
        self.assertFalse(lease.pinned())
        lease.pin("b")


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestHttp(unittest.TestCase):
    def call(self, port, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_quote_create_and_read_over_http(self):
        x = IntakeWorld()
        server = serve(x.svc, 0, None, x.intake)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            code, bad = self.call(port, "POST", "/api/v1/campaigns/quote", x.request(target=OLD_POOL.hex()))
            self.assertEqual((code, bad["success"]), (400, False))
            code, q = self.call(port, "POST", "/api/v1/campaigns/quote", x.request())
            self.assertEqual(code, 200)
            code, second = self.call(port, "POST", "/api/v1/campaigns/quote", x.request(nonce=1))
            self.assertEqual(code, 200)  # quotes share the singleton; none blocks another (CS-01)
            self.assertEqual(second["singleton"]["coin_id"], q["singleton"]["coin_id"])
            code, out = self.call(port, "POST", "/api/v1/campaigns/create", {"quote": q["quote"], "spend_bundle": x.xch_bundle(q)})
            self.assertEqual((code, out["status"]), (200, "created"))
            code, page = self.call(port, "GET", f"/api/v1/campaigns/{q['campaign']}")
            self.assertEqual((code, page["budget"], page["fee"]), (200, 970_000, 30_000))
            code, again = self.call(port, "POST", "/api/v1/campaigns/create", {"quote": "nope", "spend_bundle": None})
            self.assertEqual((code, again["requote"]), (409, True))
            code, _ = self.call(port, "GET", "/api/v1/campaigns/" + "00" * 32)
            self.assertEqual(code, 404)
            code, _ = self.call(port, "GET", "/api/v1/campaigns/zz")
            self.assertEqual(code, 400)
        finally:
            server.shutdown()
        # One client's POSTs are limited (CS-01, CS-09); reads are not.
        server = serve(x.svc, 0, None, x.intake, post_limit=PostLimit(per_minute=2))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            codes = [self.call(port, "POST", "/api/v1/campaigns/quote", x.request(nonce=10 + i))[0] for i in range(3)]
            self.assertEqual(codes, [200, 200, 429])
            self.assertEqual(self.call(port, "GET", f"/api/v1/campaigns/{q['campaign']}")[0], 200)
        finally:
            server.shutdown()
        # The page is the service's: it is served with no intake (no router key on the host).
        server = serve(x.svc, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            code, page = self.call(port, "GET", f"/api/v1/campaigns/{q['campaign']}")
            self.assertEqual((code, page["status"], page["budget"]), (200, "created", 970_000))
            code, reg = self.call(port, "GET", "/api/v1/distributors")
            self.assertEqual((code, reg["this"], reg["listed"]), (200, x.svc.launcher_id.hex(), None))
        finally:
            server.shutdown()


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestCatCreate(unittest.TestCase):
    def setUp(self):
        self.x = IntakeWorld(reward_asset=ASSET)

    def test_quote_sign_create(self):
        x = self.x
        q = x.intake.quote(x.request())
        self.assertEqual(q["conditions"]["deposit"]["asset"], ASSET.hex())
        dev_before = x.w.balance("dev")
        out = x.intake.create({"quote": q["quote"], "spend_bundle": x.cat_bundle(q)})
        self.assertEqual(out["status"], "created")
        self.assertEqual(x.w.balance("dev") - dev_before, 30_000)
        self.assertEqual(x.svc.d.reserve_coin.amount, 970_000)
        self.assertEqual(x.w.balance(x.creator.puzzle_hash), 500)  # the CAT change came back
        row = next(c for c in x.svc.status()["campaigns"] if c["id"] == q["campaign"])
        self.assertEqual(row["budget"], 970_000)

    def test_short_or_long_cat_deposits_are_refused(self):
        x = self.x
        q = x.intake.quote(x.request())
        with self.assertRaisesRegex(ValueError, "leaves 999,999"):
            x.intake.create({"quote": q["quote"], "spend_bundle": x.cat_bundle(q, leave=999_999)})
        with self.assertRaisesRegex(ValueError, "cannot burn"):
            x.intake.create({"quote": q["quote"], "spend_bundle": x.cat_bundle(q, leave=1_000_001)})
        self.assertTrue(x.svc.lease.busy())

    def test_reward_cat_must_be_in_a_v16_pool(self):
        x = self.x
        x.intake.pools = lambda: {POOL: PoolInfo(frozenset({XCH}), 17), OLD_POOL: PoolInfo(frozenset({ASSET}), 16)}
        with self.assertRaisesRegex(ValueError, "reward asset is not in any Forge registry pool"):
            x.intake.quote(x.request())


if __name__ == "__main__":
    unittest.main()
