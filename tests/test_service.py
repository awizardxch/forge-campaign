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
WINDOW = 56


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
