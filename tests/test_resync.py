"""Resync from chain: a distributor's driver state rebuilt from its launcher id
alone must equal the live driver's, after every kind of action, and must keep
working on chain. The deploy record stops being the only copy."""

import unittest

try:
    from chia_wallet_sdk import Clvm

    from flpcr.chain.auth import BySignature
    from flpcr.chain.driver import HEIGHT_WINDOW, ClaimRequest, Puzzles
    from flpcr.chain.resync import ResyncError, resync
    from flpcr.chain.sim import SimWorld

    HAVE_CHAIN = True
except (ImportError, FileNotFoundError):  # pragma: no cover
    HAVE_CHAIN = False

from flpcr import EngineState, FixtureSource, HolderEvent, Kind, Params, run_epoch

POOL, LP = b"\xb0" * 32, b"\x1b" * 32
WINDOW = 56


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestResync(unittest.TestCase):
    ASSET = bytes(32)  # XCH; TestResyncCat replays a CAT-reward distributor

    def setUp(self):
        w = self.w = SimWorld(delegate_fee=0, reward_asset=self.ASSET)
        w.advance_to(w.h + 3)
        k = w.keys
        self.alice, self.bob, self.carol = k.ph("alice"), k.ph("bob"), k.ph("carol")
        self.wallets = [w.bls() for _ in range(6)]
        holders = [(self.alice, 3), (self.bob, 1), (self.carol, 2)] + [(x.puzzle_hash, 1 + i % 3) for i, x in enumerate(self.wallets)]
        self.src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, ph, n) for ph, n in holders]})
        start = w.h + HEIGHT_WINDOW + 2
        self.c1 = w.campaign("creator", 1_000_000, start, start + 40, Kind.LP_HOLD, POOL, claim_window=WINDOW)
        self.c2 = w.campaign("creator", 2_000_000, start, start + 40, Kind.LP_HOLD, POOL, claim_window=WINDOW, nonce=3,
                             params=Params(blacklist=frozenset({self.carol})))
        w.create(self.c1, "creator")
        w.create(self.c2, "creator")
        self.engine = EngineState(w.launcher_id, height=start)

    def root(self, end):
        w = self.w
        w.advance_to(end + 1)
        self.engine, r = run_epoch(self.engine, [self.c1, self.c2], self.src, end)
        w.post(r)
        w.advance_to(w.h + HEIGHT_WINDOW + w.cfg.dispute_blocks)
        return r

    def assert_same(self):
        got = resync(self.w.sim, Puzzles(Clvm()), self.w.launcher_id)
        live, back = self.w.d.snapshot(), got.distributor.snapshot()
        for key in live:
            self.assertEqual(back[key], live[key], key)
        self.assertEqual(got.distributor.excluded, self.w.d.excluded)
        return got

    def test_a_full_history_replays_exactly(self):
        w = self.w
        r1 = self.root(self.c1.start + 20)
        w.claim(r1, self.c1.id, "alice")                     # a first claim (insert)
        w.claim(r1, self.c2.id, "bob")
        w.set_delegate("bob", self.alice)                    # a delegation slot
        r2 = self.root(self.c1.end)
        w.claim(r2, self.c1.id, "alice")                     # a repeat claim (own slot)
        w.claim_delegated(r2, self.c1.id, self.bob, "alice")  # a delegated claim (use counter)
        # Six first claims in one bundle of chained generations, by key signature.
        reqs = [ClaimRequest(self.c1.id, x.puzzle_hash, r2.claim_for(self.c1.id, x.puzzle_hash).cumulative,
                             r2.claim_for(self.c1.id, x.puzzle_hash).proof, BySignature(x.pk.to_bytes()))
                for x in self.wallets]
        batch = w.d.sequence(w.h, reqs)
        self.assertGreater(len(batch.generations), 1)
        w.push_batch(batch)
        w.rotate("engine2")                                  # a pending rotation
        got = self.assert_same()
        self.assertGreater(got.generations, 10)
        self.assertEqual(set(got.campaigns), {self.c1.id.hex(), self.c2.id.hex()})
        self.assertEqual(got.campaigns[self.c2.id.hex()]["excluded"], [self.carol.hex()])
        self.assertEqual(sorted(got.roots), ["1", "2"])
        self.assertEqual(got.roots["2"]["totals"][self.c1.id.hex()], r2.totals[self.c1.id])

    def test_disputes_and_reclaim_replay(self):
        """Root 2 is disputed and the dispute rejected (the bond is forfeited and the
        window restarts); the creator later reclaims. Every one of those leaves replays."""
        w = self.w
        r1 = self.root(self.c1.start + 20)
        w.claim(r1, self.c1.id, "alice")
        w.advance_to(self.c1.end + 1)
        self.engine, r2 = run_epoch(self.engine, [self.c1, self.c2], self.src, self.c1.end)
        w.post(r2)
        w.dispute(r2.root_id)
        w.resolve(0)
        w.advance_to(w.h + HEIGHT_WINDOW + w.cfg.dispute_blocks)
        w.claim(r2, self.c1.id, "bob")
        w.advance_to(self.c1.end + self.c1.claim_window + 1)
        w.reclaim(self.c1.id)
        got = self.assert_same()
        self.assertTrue({"dispute", "resolve", "reclaim", "post_root"} <= {a.leaf for a in got.actions})

    def test_a_delegated_reclaim_replays(self):
        """The creator's delegate reclaims: the reclaim leaf spends a delegation slot too."""
        w = self.w
        hot = w.bls()
        w.set_delegate("creator", hot.puzzle_hash)
        r1 = self.root(self.c1.end)
        w.claim(r1, self.c1.id, "alice")
        w.advance_to(self.c1.end + self.c1.claim_window + 1)
        w.reclaim(self.c1.id, delegate=hot)
        got = self.assert_same()
        key = w.d.delegation_key(w.keys.ph("creator"), hot.puzzle_hash)
        self.assertEqual(got.distributor.delegations[key].uses, 1)

    def test_the_resynced_driver_keeps_working(self):
        """Drop the live driver; carry on from the chain alone."""
        w = self.w
        r1 = self.root(self.c1.end)
        w.claim(r1, self.c1.id, "alice")
        w.d = resync(w.sim, Puzzles(Clvm()), w.launcher_id).distributor
        self.assertGreater(w.claim(r1, self.c1.id, "bob"), 0)   # a first claim: needs the right neighbours
        self.assertEqual(w.claim(r1, self.c2.id, "alice"), r1.claim_for(self.c2.id, self.alice).cumulative)

    def test_an_unknown_launcher(self):
        with self.assertRaises(ResyncError):
            resync(self.w.sim, Puzzles(Clvm()), b"\x42" * 32)


class TestResyncCat(TestResync):
    """The same histories with a CAT reward: the reserve is found from the first spend
    and carries its lineage, and the resynced driver keeps paying the CAT."""

    ASSET = bytes([0xA5]) * 32


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestResyncRefusals(unittest.TestCase):
    def test_launched_but_never_spent(self):
        w = SimWorld(init=False)
        with self.assertRaisesRegex(ResyncError, "never spent"):
            resync(w.sim, Puzzles(Clvm()), w.launcher_id)


if __name__ == "__main__":
    unittest.main()
