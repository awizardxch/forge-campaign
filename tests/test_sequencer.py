"""The claim sequencer on the simulator: wallets claim by key signature and pay the fee from
a coin of their own, a lock (any puzzle) claims by a mode-16 message in its own spend, and
every bad request is refused at intake, never inside a batch."""

import json
import threading
import time
import types
import unittest
import urllib.request

try:
    from chia_wallet_sdk import Clvm, SecretKey, Signature

    from flpcr.chain.driver import HEIGHT_WINDOW
    from flpcr.chain.signer import KeySigner
    from flpcr.chain.sim import SimWorld
    from flpcr.chain.values import to_program
    from flpcr.service.chain import SimChain, bundle_json
    from flpcr.service.core import CampaignService, OperatorKey
    from flpcr.service.sequencer import FEE_PER_CLAIM, ClaimSequencer

    HAVE_CHAIN = True
except (ImportError, FileNotFoundError):  # pragma: no cover
    HAVE_CHAIN = False

from flpcr import FixtureSource, HolderEvent, Kind

POOL, LP = b"\xb0" * 32, b"\x1b" * 32


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestSequencer(unittest.TestCase):
    def setUp(self):
        sk = SecretKey.from_seed(bytes([7]) * 32).derive_synthetic()
        op = OperatorKey(sk)
        w = self.w = SimWorld(engine_ph=op.puzzle_hash)
        w.wallets[op.puzzle_hash] = types.SimpleNamespace(sk=sk, pk=sk.public_key(), puzzle_hash=op.puzzle_hash)
        w.sim.new_coin(op.puzzle_hash, 10**13)
        w.advance_to(w.h + 3)
        self.holders = [w.sim.bls(0) for _ in range(3)]  # no coins: they claim by key
        self.lock_ph = w.keys.ph("lock")  # any puzzle: stands in for a multisig lock
        events = [HolderEvent(0, x.puzzle_hash, 1 + i) for i, x in enumerate(self.holders)] + [HolderEvent(0, self.lock_ph, 2)]
        self.src = FixtureSource(lp_assets={POOL: LP}, events={LP: events})
        start = w.h + HEIGHT_WINDOW + 2
        self.c = w.campaign("creator", 1_000_000, start, start + 40, Kind.LP_HOLD, POOL, claim_window=160)
        w.create(self.c, "creator")
        self.svc = CampaignService(SimChain(w.sim), w.launcher_id, "testnet11", lambda cs: self.src, operator=op, every=10)
        self.svc.boot()
        w.advance_to(self.c.end + 1)
        self.svc.operator_step(wait=5, poll=0)
        w.advance_to(self.svc.status()["pending"]["activates_at"] + 1)
        self.seq = ClaimSequencer(self.svc)
        self.funder = w.sim.bls(10**12)

    def leaf(self, ph):
        return next(x for x in self.svc.claimable(ph)["claims"] if x["campaign"] == self.c.id.hex())

    def fee_bundle(self, fee, extra_spends=(), extra_sig=None):
        clvm = Clvm()
        coin = self.w.sim.new_coin(self.funder.puzzle_hash, 10**11)
        clvm.spend_standard_coin(coin, self.funder.pk, clvm.delegated_spend(
            [to_program(clvm, [51, self.funder.puzzle_hash, coin.amount - fee])]))
        spends = list(clvm.coin_spends()) + list(extra_spends)
        sig = KeySigner([self.funder.sk], bytes.fromhex("37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615")).sign(spends)
        if extra_sig is not None:
            sig = Signature.aggregate([sig, extra_sig])
        return bundle_json(spends, sig)

    def key_request(self, wallets, fee=None):
        claims, sigs = [], []
        for x in wallets:
            claims.append({"campaign": self.c.id.hex(), "puzzle_hash": x.puzzle_hash.hex(), "public_key": x.pk.to_bytes().hex()})
            sigs.append(x.sk.sign(bytes.fromhex(self.leaf(x.puzzle_hash)["leaf"])))
        return {"claims": claims, "signature": Signature.aggregate(sigs).to_bytes().hex(),
                "spend_bundle": self.fee_bundle(FEE_PER_CLAIM * len(wallets) if fee is None else fee)}

    def lock_request(self, message=None, fee=FEE_PER_CLAIM):
        leaf = bytes.fromhex(self.leaf(self.lock_ph)["leaf"])
        coin = self.w.sim.new_coin(self.lock_ph, 10**9)
        conds = [[66, 16, b"k" + leaf if message is None else message], [51, self.lock_ph, coin.amount - fee]]
        spend = self.w.keys.spend(coin, "lock", conds)
        return {"claims": [{"campaign": self.c.id.hex(), "puzzle_hash": self.lock_ph.hex()}],
                "spend_bundle": bundle_json([spend], Signature.infinity())}

    def balance(self, ph):
        return sum(c.amount for c in self.w.sim.unspent_coins(ph, False))

    def test_wallets_and_a_lock_in_one_bundle(self):
        owed = {x.puzzle_hash: self.leaf(x.puzzle_hash)["claimable"] for x in self.holders}
        owed[self.lock_ph] = self.leaf(self.lock_ph)["claimable"]
        before = {ph: self.balance(ph) for ph in owed}
        a = self.seq.submit(self.key_request(self.holders[:2]))
        b = self.seq.submit(self.key_request(self.holders[2:]))
        c = self.seq.submit(self.lock_request())
        out = self.seq.run_batch(wait=5, poll=0)
        self.assertTrue(out["pushed"], out)
        self.assertEqual(out["claims"], 4)
        for req in (a, b, c):
            self.assertEqual(self.seq.status_of(req["request"])["status"], "confirmed")
        # The lock's request spent a coin minted after `before` was read: its change comes back too.
        change = {self.lock_ph: 10**9 - FEE_PER_CLAIM}
        for ph, amount in owed.items():
            self.assertEqual(self.balance(ph) - before[ph], amount + change.get(ph, 0), ph.hex()[:8])
        self.assertEqual(self.leaf(self.holders[0].puzzle_hash)["claimable"], 0)

    def test_one_combined_signature(self):
        """A wallet that signs the claim stand-in and the fee spend in one chip0002_signCoinSpends
        request returns one aggregate: sent as `signature` with the fee bundle's own left empty,
        it is checked against the leaves and the fee spend together, and the claim is paid."""
        x = self.holders[0]
        req = self.key_request([x])
        fee_only = req["spend_bundle"]["aggregated_signature"]
        combined = Signature.aggregate([Signature.from_bytes(bytes.fromhex(req["signature"])),
                                        Signature.from_bytes(bytes.fromhex(fee_only.removeprefix("0x")))])
        req["signature"] = combined.to_bytes().hex()
        req["spend_bundle"] = {**req["spend_bundle"], "aggregated_signature": ""}
        owed = self.leaf(x.puzzle_hash)["claimable"]
        before = self.balance(x.puzzle_hash)
        self.seq.submit(req)
        out = self.seq.run_batch(wait=5, poll=0)
        self.assertTrue(out["pushed"], out)
        self.assertEqual(self.balance(x.puzzle_hash) - before, owed)
        # The leaf signature alone, presented as the combined one, is refused: the fee spend's is missing.
        y = self.holders[1]
        short = self.key_request([y])
        short["spend_bundle"] = {**short["spend_bundle"], "aggregated_signature": ""}
        with self.assertRaisesRegex(ValueError, "combined signature does not verify"):
            self.seq.submit(short)

    def test_claimable_for_a_whole_wallet(self):
        """`claimable_many`: one request for every address a wallet scan holds, answering only the
        addresses with something to claim (the 🪎 tab)."""
        stranger = self.w.sim.bls(0)
        phs = [stranger.puzzle_hash, self.holders[0].puzzle_hash, self.lock_ph, self.holders[0].puzzle_hash]
        out = self.svc.claimable_many(phs)
        self.assertEqual([h["puzzle_hash"] for h in out["holders"]], [self.holders[0].puzzle_hash.hex(), self.lock_ph.hex()])
        self.assertEqual(out["holders"][0]["claims"][0]["claimable"], self.leaf(self.holders[0].puzzle_hash)["claimable"])
        with self.assertRaisesRegex(ValueError, "at most"):
            self.svc.claimable_many([stranger.puzzle_hash] * (self.svc.MAX_CLAIMABLE_BATCH + 1))

    def test_a_wallet_lookup_reads_the_node_once_and_never_under_the_lock(self):
        """Audit 2026-11-10, CS-03. `claimable` called the node for the peak under the service
        lock, once per address: a 6,000-address lookup held every read for about 15 minutes."""
        svc = self.svc
        real, calls = svc.chain.peak, []

        def peak():
            calls.append(svc.lock._is_owned())
            return real()

        svc.chain.peak = peak
        try:
            phs = [bytes([i]) * 32 for i in range(1, 200)] + [self.holders[0].puzzle_hash]
            out = svc.claimable_many(phs)
            self.assertEqual(len(out["holders"]), 1)
            self.assertEqual(calls, [False])  # one node call for the whole request, lock not held
            calls.clear()
            self.assertTrue(svc.claimable(self.holders[0].puzzle_hash)["claims"])
            svc.status()
            self.assertEqual(calls, [False, False])
        finally:
            svc.chain.peak = real

    def poisoned(self, wallet, extra_condition):
        """A key request whose fee spend also carries `extra_condition`."""
        req = self.key_request([wallet])
        clvm = Clvm()
        coin = self.w.sim.new_coin(self.funder.puzzle_hash, 10**11)
        conds = [[51, self.funder.puzzle_hash, coin.amount - FEE_PER_CLAIM], extra_condition]
        clvm.spend_standard_coin(coin, self.funder.pk, clvm.delegated_spend([to_program(clvm, c) for c in conds]))
        spends = list(clvm.coin_spends())
        sig = KeySigner([self.funder.sk], bytes.fromhex("37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615")).sign(spends)
        req["spend_bundle"] = bundle_json(spends, sig)
        return req

    def test_a_request_the_chain_refuses_does_not_sink_the_others(self):
        """Audit 2026-11-10, CS-02. A fee spend may be any puzzle, so intake cannot see every
        condition the chain will refuse (here ASSERT_BEFORE_HEIGHT_ABSOLUTE 1). The batch it
        rides in fails once; after that each request is pushed alone, the honest ones are paid
        and the bad one is refused for good."""
        honest = [self.seq.submit(self.key_request([x])) for x in self.holders[:2]]
        bad = self.seq.submit(self.poisoned(self.holders[2], [87, 1]))
        self.assertFalse(self.seq.run_batch(wait=5, poll=0)["pushed"])
        for _ in range(6):
            self.seq.run_batch(wait=5, poll=0)
        for req in honest:
            self.assertEqual(self.seq.status_of(req["request"])["status"], "confirmed")
        status = self.seq.status_of(bad["request"])
        self.assertEqual(status["status"], "refused")
        self.assertIn("on its own", status["detail"])
        # A later request is batched normally again.
        later = self.seq.submit(self.lock_request())
        self.assertTrue(self.seq.run_batch(wait=5, poll=0)["pushed"])
        self.assertEqual(self.seq.status_of(later["request"])["status"], "confirmed")

    def test_one_fee_coin_pays_for_one_request(self):
        """CS-02: two queued requests sharing a fee spend were a double spend in their batch."""
        first = self.key_request(self.holders[:1])
        second = self.key_request(self.holders[1:2])
        second["spend_bundle"] = first["spend_bundle"]
        self.seq.submit(first)
        with self.assertRaisesRegex(ValueError, "already pays for another queued request"):
            self.seq.submit(second)

    def test_a_fee_coin_spent_after_intake_refuses_only_its_request(self):
        """CS-02: the claimant spends the fee coin elsewhere before the batch is built."""
        a = self.seq.submit(self.key_request(self.holders[:1]))
        req = self.key_request(self.holders[1:2])
        b = self.seq.submit(req)
        coin_spend = self.seq.queue[b["request"]].spends[0]
        clvm = Clvm()
        clvm.spend_standard_coin(coin_spend.coin, self.funder.pk, clvm.delegated_spend(
            [to_program(clvm, [51, self.funder.puzzle_hash, coin_spend.coin.amount])]))
        self.w.sim.spend_coins(list(clvm.coin_spends()), [self.funder.sk])
        out = self.seq.run_batch(wait=5, poll=0)
        self.assertTrue(out["pushed"], out)
        self.assertEqual(self.seq.status_of(a["request"])["status"], "confirmed")
        status = self.seq.status_of(b["request"])
        self.assertEqual(status["status"], "refused")
        self.assertIn("spent after", status["detail"])

    def test_refused_at_intake(self):
        x, y = self.holders[0], self.holders[1]
        forged = self.key_request([x])
        forged["signature"] = y.sk.sign(b"not the leaf").to_bytes().hex()
        with self.assertRaisesRegex(ValueError, "does not verify"):
            self.seq.submit(forged)
        wrong_key = self.key_request([x])
        wrong_key["claims"][0]["public_key"] = y.pk.to_bytes().hex()
        with self.assertRaisesRegex(ValueError, "does not own"):
            self.seq.submit(wrong_key)
        with self.assertRaisesRegex(ValueError, "need"):
            self.seq.submit(self.key_request([x], fee=1))
        with self.assertRaisesRegex(ValueError, "no spend sends the claim message"):
            self.seq.submit(self.lock_request(message=b"k" + b"\x00" * 32))
        stranger = self.w.sim.bls(0)
        with self.assertRaisesRegex(ValueError, "nothing to claim"):
            self.seq.submit({"claims": [{"campaign": self.c.id.hex(), "puzzle_hash": stranger.puzzle_hash.hex(),
                                         "public_key": stranger.pk.to_bytes().hex()}], "signature": "", "spend_bundle": None})
        self.assertEqual(self.seq.queue, {})

    def test_a_coin_that_is_not_on_chain(self):
        req = self.key_request([self.holders[0]])
        req["spend_bundle"]["coin_spends"][0]["coin"]["amount"] = 10**11 + 1  # names a coin that does not exist
        with self.assertRaisesRegex(ValueError, "not an unspent coin|does not verify"):
            self.seq.submit(req)

    def test_claimed_twice_is_refused_after_the_first_lands(self):
        self.seq.submit(self.key_request([self.holders[0]]))
        self.seq.run_batch(wait=5, poll=0)
        with self.assertRaisesRegex(ValueError, "nothing to claim"):
            self.seq.submit(self.key_request([self.holders[0]]))

    # --- a claim pushes on request (owner, 2026-11-27) ----------------------------------------

    def until(self, test, seconds=20):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if test():
                return True
            time.sleep(0.05)
        return False

    def test_a_kick_pays_a_request_without_the_loop(self):
        """The claimant paid the fee: kick() pays the request on its own thread, no loop pass."""
        x = self.holders[0]
        owed, before = self.leaf(x.puzzle_hash)["claimable"], self.balance(x.puzzle_hash)
        req = self.seq.submit(self.key_request([x]))
        self.assertTrue(self.seq.kick(wait=5, poll=0))
        self.assertTrue(self.until(lambda: self.seq.status_of(req["request"])["status"] == "confirmed"),
                        self.seq.status_of(req["request"]))
        self.assertTrue(self.until(lambda: not self.seq._kicking))
        self.assertEqual(self.balance(x.puzzle_hash) - before, owed)

    def test_a_kick_while_one_runs_gets_its_own_batch(self):
        """A request queued while a kick's batch waits for its block is paid by one more batch
        after it, with no loop pass and no second thread."""
        a, b = self.holders[0], self.holders[1]
        second_req = self.key_request([b])  # built now: its leaf is read before the first lands
        first = self.seq.submit(self.key_request([a]))
        seen = {}
        real_wait = self.svc._wait

        def wait_then_submit(coin_id, wait, poll):  # the first batch has read the queue: queue another
            if "second" not in seen:
                seen["second"] = self.seq.submit(second_req)
                seen["kicked"] = self.seq.kick(wait=5, poll=0)  # one at a time: asks for one more
            return real_wait(coin_id, wait, poll)

        self.svc._wait = wait_then_submit
        try:
            self.assertTrue(self.seq.kick(wait=5, poll=0))
            self.assertTrue(self.until(lambda: not self.seq._kicking))
        finally:
            self.svc._wait = real_wait
        self.assertFalse(seen["kicked"])
        self.assertEqual(self.seq.status_of(first["request"])["status"], "confirmed")
        self.assertEqual(self.seq.status_of(seen["second"]["request"])["status"], "confirmed",
                         self.seq.status_of(seen["second"]["request"]))

    def test_a_batch_waiting_on_the_lock_never_refuses_what_another_paid(self):
        """Two batches at once (the loop's and a kick's): the one that waited on the write lock
        must not read the other's paid requests, whose fee coins are spent now, as refused."""
        req = self.seq.submit(self.key_request([self.holders[0]]))
        out = {}
        with self.svc.write_lock:  # the waiting batch reads the queue, then blocks here
            t = threading.Thread(target=lambda: out.setdefault("late", self.seq.run_batch(wait=5, poll=0)))
            t.start()
            time.sleep(0.3)
            out["first"] = self.seq.run_batch(wait=5, poll=0)  # the lock is reentrant: this one pays
        t.join(20)
        self.assertTrue(out["first"]["pushed"], out)
        self.assertIsNone(out["late"], out)
        self.assertEqual(self.seq.status_of(req["request"])["status"], "confirmed")

    def test_a_syncing_node_leaves_the_request_queued(self):
        """NO_TRANSACTIONS_WHILE_SYNCING is the node's state: the request is neither suspect nor
        refused, however often it happens, and is paid once the node takes transactions."""
        from flpcr.service.chain import NodeBusy, push_refusal

        self.assertIsInstance(push_refusal("Failed to include transaction 56f8, error NO_TRANSACTIONS_WHILE_SYNCING"), NodeBusy)
        self.assertNotIsInstance(push_refusal("Failed to include transaction 56f8, error DOUBLE_SPEND"), NodeBusy)
        req = self.seq.submit(self.key_request([self.holders[0]]))
        real_push, refusals = self.svc.chain.push, []

        def syncing(spends, signature):
            if len(refusals) < 4:
                refusals.append(1)
                raise push_refusal("Failed to include transaction 56f8, error NO_TRANSACTIONS_WHILE_SYNCING")
            return real_push(spends, signature)

        self.svc.chain.push = syncing
        try:
            for _ in range(4):
                out = self.seq.run_batch(wait=5, poll=0)
                self.assertEqual((out["pushed"], out.get("retry")), (False, True), out)
                st = self.seq.status_of(req["request"])
                self.assertEqual(st["status"], "queued", st)
                self.assertFalse(self.seq.queue[req["request"]].suspect)
            self.assertTrue(self.seq.run_batch(wait=5, poll=0)["pushed"])
        finally:
            self.svc.chain.push = real_push
        self.assertEqual(self.seq.status_of(req["request"])["status"], "confirmed")

    def test_a_quiet_mempool_takes_a_small_fee(self):
        """While the mempool is quiet the node takes a fee under 5 mojos per cost: a claim paying
        the least the service asks (1 mojo per cost) is pushed and paid."""
        seq = ClaimSequencer(self.svc, mempool_fill=lambda: 0.2)
        req = seq.submit(self.key_request([self.holders[0]], fee=FEE_PER_CLAIM))
        self.assertTrue(seq.run_batch(wait=5, poll=0)["pushed"])
        self.assertEqual(seq.status_of(req["request"])["status"], "confirmed")
        self.assertFalse(seq.terms()["busy"])
        with self.assertRaisesRegex(ValueError, "need"):  # nothing under the least it asks
            seq.submit(self.key_request([self.holders[1]], fee=FEE_PER_CLAIM - 1))

    def test_a_fee_under_the_network_floor_is_refused_before_it_is_pushed(self):
        """While the mempool is busy, a bundle paying a nonzero fee under 5 mojos per cost would be
        refused or parked, and the singleton would wait on it: the sequencer refuses the request
        that pays under its share, and pushes the rest."""
        from flpcr.service.sequencer import CLAIM_COST, MIN_FEE_PER_COST
        seq = ClaimSequencer(self.svc, fee_per_claim=1, mempool_fill=lambda: 0.9)  # busy
        cheap = seq.submit(self.key_request([self.holders[0]], fee=100_000_000))
        fair = seq.submit(self.key_request([self.holders[1]], fee=MIN_FEE_PER_COST * CLAIM_COST))
        pushed, costs = [], []
        real = self.svc.chain.push

        def watch(spends, sig):
            costs.append(self.svc.d.spend_cost(list(spends)))
            pushed.append(len(spends))
            return real(spends, sig)

        self.svc.chain.push = watch
        try:
            out = seq.run_batch(wait=5, poll=0)
            self.assertEqual((out["pushed"], out.get("retry")), (False, True), out)
            self.assertEqual(pushed, [], "nothing is pushed while a request pays under the floor")
            st = seq.status_of(cheap["request"])
            self.assertEqual(st["status"], "refused")
            self.assertIn("under the network's floor", st["detail"])
            self.assertEqual(seq.status_of(fair["request"])["status"], "queued")
            self.assertTrue(seq.run_batch(wait=5, poll=0)["pushed"])
        finally:
            self.svc.chain.push = real
        self.assertEqual(seq.status_of(fair["request"])["status"], "confirmed")
        self.assertLessEqual(costs[0], CLAIM_COST, "a claim costs no more than the page sizes its fee for")
        self.assertTrue(seq.terms()["busy"])
        self.assertEqual(self.seq.terms()["min_fee_per_claim"], FEE_PER_CLAIM)

    def test_the_claims_route_pushes_at_once(self):
        """POST /api/v1/claims on a sequencer that batches on submit: confirmed with no run_batch call."""
        from flpcr.service.http import PostLimit, serve

        seq = ClaimSequencer(self.svc, batch_on_submit=True)
        server = serve(self.svc, 0, seq, post_limit=PostLimit(1000))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            body = json.dumps(self.key_request([self.holders[2]])).encode()
            r = urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/claims", data=body, headers={"Content-Type": "application/json"}))
            req = json.loads(r.read())
            self.assertEqual(r.status, 202)
            self.assertTrue(self.until(lambda: seq.status_of(req["request"])["status"] == "confirmed"),
                            seq.status_of(req["request"]))
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
