"""The hosted service on the simulator: it boots from the chain alone, serves
the same proofs the engine posted, and its operator posts roots signed with
its own key, exactly as it will when hosted."""

import types
import unittest

try:
    from chia_wallet_sdk import SecretKey, standard_puzzle_hash

    from flpcr.chain.driver import HEIGHT_WINDOW
    from flpcr.chain.sim import SimWorld
    from flpcr.service.chain import SimChain
    from flpcr.service.core import CampaignService, OperatorKey

    HAVE_CHAIN = True
except (ImportError, FileNotFoundError):  # pragma: no cover
    HAVE_CHAIN = False

from flpcr import EngineState, FixtureSource, HolderEvent, Kind, Params, run_epoch

POOL, LP = b"\xb0" * 32, b"\x1b" * 32
WINDOW = 160


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestService(unittest.TestCase):
    def setUp(self):
        sk = SecretKey.from_seed(bytes([7]) * 32).derive_synthetic()
        self.op = OperatorKey(sk)
        w = self.w = SimWorld(engine_ph=self.op.puzzle_hash)
        # The simulator's own message helper spends from registered wallets: register the operator.
        w.wallets[self.op.puzzle_hash] = types.SimpleNamespace(sk=sk, pk=sk.public_key(), puzzle_hash=self.op.puzzle_hash)
        w.sim.new_coin(self.op.puzzle_hash, 10**13)  # the operator's fee wallet
        w.advance_to(w.h + 3)
        self.alice, self.bob = w.keys.ph("alice"), w.keys.ph("bob")
        self.src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, self.alice, 3), HolderEvent(0, self.bob, 1)]})
        start = w.h + HEIGHT_WINDOW + 2
        self.c = w.campaign("creator", 1_000_000, start, start + 40, Kind.LP_HOLD, POOL, claim_window=WINDOW,
                            params=Params(blacklist=frozenset({b"\x99" * 32})))
        w.create(self.c, "creator")

    def service(self, **kw):
        svc = CampaignService(SimChain(self.w.sim), self.w.launcher_id, "testnet11", lambda cs: self.src,
                              operator=self.op, every=kw.pop("every", 10), **kw)
        svc.boot()
        return svc

    def test_boot_rebuilds_the_posted_roots(self):
        """Roots the old operator posted are rebuilt by the engine and match the chain."""
        w = self.w
        w.advance_to(self.c.start + 21)
        engine, r1 = run_epoch(EngineState(w.launcher_id, height=self.c.start), [self.c], self.src, self.c.start + 20)
        w.post(r1)
        svc = self.service()
        self.assertIsNone(svc.s.mismatch)
        self.assertEqual(svc.s.roots[1].root, r1.root)
        self.assertEqual(svc.d.snapshot(), w.d.snapshot())

    def test_operator_posts_and_holders_claim(self):
        w = self.w
        svc = self.service()
        w.d = svc.d  # one driver: the service's
        self.assertIsNone(svc.operator_step(wait=5, poll=0))  # nothing due before the campaign starts
        w.advance_to(self.c.start + 12)
        posted = svc.operator_step(wait=5, poll=0)
        self.assertEqual(posted["root_id"], 1)
        st = svc.status()
        self.assertEqual(st["pending"]["root_id"], 1)
        w.advance_to(st["pending"]["activates_at"] + 1)
        mine = svc.claimable(self.alice)
        self.assertEqual(len(mine["claims"]), 1)
        cl = mine["claims"][0]
        root = svc.s.roots[1]
        self.assertGreater(w.claim(root, self.c.id, "alice"), 0)
        self.assertEqual(svc.claimable(self.alice)["claims"][0]["claimable"], 0)  # paid: nothing more until the next root
        self.assertEqual(cl["excluded"], [(b"\x99" * 32).hex()])

    def test_reads_answer_while_a_post_waits_to_confirm(self):
        """Status, claimable and the campaign page answer while the operator's post sits in its
        confirmation wait (2026-11-10, testnet: every read stalled for minutes behind it)."""
        import threading
        import time

        w = self.w
        svc = self.service()
        w.d = svc.d
        w.advance_to(self.c.start + 12)
        in_wait, release = threading.Event(), threading.Event()
        real_wait = svc._wait

        def held_wait(coin_id, wait, poll):
            in_wait.set()
            release.wait(10)
            return real_wait(coin_id, wait, poll)

        svc._wait = held_wait
        posted = {}
        builder = threading.Thread(target=lambda: posted.update(svc.operator_step(wait=5, poll=0) or {}))
        builder.start()
        self.assertTrue(in_wait.wait(10), "the post never reached its confirmation wait")
        t0 = time.monotonic()
        st = svc.status()
        svc.claimable(self.alice)
        svc.campaign(self.c.id)
        took = time.monotonic() - t0
        release.set()
        builder.join(10)
        self.assertLess(took, 2.0, f"reads waited {took:.1f} s behind the post")
        self.assertIsNone(st["pending"], "a read during the wait sees the last confirmed state")
        self.assertEqual(posted.get("root_id"), 1)
        self.assertEqual(svc.status()["pending"]["root_id"], 1, "and the post is applied once it confirms")

    def test_a_restarted_service_carries_on(self):
        """Post with one service, restart a fresh one from chain, and it posts the next root."""
        w = self.w
        svc = self.service()
        w.advance_to(self.c.start + 12)
        svc.operator_step(wait=5, poll=0)
        w.advance_to(svc.status()["pending"]["activates_at"] + 12)
        again = self.service()
        self.assertIsNone(again.s.mismatch)
        self.assertEqual(sorted(again.s.roots), [1])
        posted = again.operator_step(wait=5, poll=0)
        self.assertEqual(posted["root_id"], 2)

    def test_the_campaign_page_and_pending_amounts(self):
        """The tab's two reads: a campaign's page, and what a holder has that is not claimable yet."""
        w = self.w
        svc = self.service()
        w.d = svc.d
        self.assertIsNone(svc.campaign(b"\x00" * 32))
        page = svc.campaign(self.c.id)
        self.assertEqual((page["phase"], page["emittable"], page["roots"], page["holders"]), ("scheduled", 0, [], 0))
        self.assertEqual((page["amount"], page["fee"], page["budget"]), (1_000_000, 30_000, 970_000))
        self.assertEqual(page["reclaim_from"], self.c.end + WINDOW)

        w.advance_to(self.c.start + 12)
        svc.operator_step(wait=5, poll=0)
        page = svc.campaign(self.c.id)
        self.assertEqual([(r["root_id"], r["state"], r["holders"]) for r in page["roots"]], [(1, "pending", 2)])
        self.assertEqual(page["allocated"], page["roots"][0]["total"])
        self.assertLessEqual(page["allocated"], page["emittable"])  # the puzzle's ceiling
        mine = svc.claimable(self.alice)
        self.assertEqual(mine["claims"], [])  # in its dispute window: no proof handed out
        self.assertEqual([(x["root_id"], x["disputed"]) for x in mine["pending"]], [(1, False)])
        first = mine["pending"][0]["amount"]
        self.assertGreater(first, 0)

        w.advance_to(svc.status()["pending"]["activates_at"] + 1)
        mine = svc.claimable(self.alice)
        self.assertEqual((mine["pending"], mine["claims"][0]["claimable"], mine["claims"][0]["claimed"]), ([], first, 0))
        self.assertEqual(w.claim(svc.s.roots[1], self.c.id, "alice"), first)
        self.assertEqual(svc.claimable(self.alice)["claims"][0]["claimed"], first)

        w.advance_to(w.h + 12)
        svc.operator_step(wait=5, poll=0)  # root 2: pending over the active root 1
        mine = svc.claimable(self.alice)
        second = svc.s.roots[2].claim_for(self.c.id, self.alice).cumulative
        self.assertEqual(mine["pending"][0]["amount"], second - first)  # only what root 2 adds
        self.assertEqual([r["state"] for r in svc.campaign(self.c.id)["roots"]], ["active", "pending"])
        self.assertEqual(svc.claimable(b"\x77" * 32), {"root_id": 1, "claims": [], "pending": []})

    def test_a_wrong_key_does_not_post(self):
        w = self.w
        other = OperatorKey(SecretKey.from_seed(bytes([8]) * 32).derive_synthetic())
        w.sim.new_coin(other.puzzle_hash, 10**13)
        svc = CampaignService(SimChain(w.sim), w.launcher_id, "testnet11", lambda cs: self.src, operator=other, every=10)
        svc.boot()
        w.advance_to(self.c.start + 12)
        with self.assertRaisesRegex(RuntimeError, "not this distributor's engine"):
            svc.operator_step(wait=5, poll=0)


if __name__ == "__main__":
    unittest.main()
