"""The operator's reclaim guard (scripts/deploy_testnet.py reclaim_blockers):
the puzzle lets a creator reclaim from end + claim_window, and by design what
nobody claims goes back to them. The guard keeps a reclaim from landing in the
gap between a root being posted and its holders being able to claim it,
which once nearly returned a whole root's rewards to the creator on testnet."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

try:
    import deploy_testnet as dep
    from flpcr.chain.sim import SimWorld

    HAVE = True
except (ImportError, SystemExit):  # pragma: no cover - needs sage_rpc and the SDK
    HAVE = False

from flpcr import EngineState, FixtureSource, HolderEvent, Kind, run_epoch
from flpcr.chain.driver import HEIGHT_WINDOW

POOL, LP = b"\xb0" * 32, b"\x1b" * 32


@unittest.skipUnless(HAVE, "deploy_testnet or the SDK missing")
class TestReclaimGuard(unittest.TestCase):
    def setUp(self):
        w = self.w = SimWorld()
        w.advance_to(w.h + 3)
        self.alice, self.bob = w.keys.ph("alice"), w.keys.ph("bob")
        self.src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, self.alice, 3), HolderEvent(0, self.bob, 1)]})
        self.c = w.campaign("creator", 1_000_000, w.h + 34, w.h + 74, Kind.LP_HOLD, POOL, claim_window=56)
        w.create(self.c, "creator")
        self.state = {"roots": {}}
        self.engine = EngineState(w.launcher_id, height=self.c.start)

    def post(self, end):
        w = self.w
        w.advance_to(end + 1)
        self.engine, root = run_epoch(self.engine, [self.c], self.src, end)
        w.post(root)
        self.state["roots"][str(root.root_id)] = {
            "start": root.start, "end": root.end, "activates_at": w.h + HEIGHT_WINDOW + w.cfg.dispute_blocks,
            "totals": {k.hex(): v for k, v in root.totals.items()},
            "claims": [{"campaign": cl.campaign_id.hex(), "puzzle_hash": cl.puzzle_hash.hex(), "cumulative": cl.cumulative}
                       for cl in root.claims],
        }
        return root

    def blockers(self):
        return dep.reclaim_blockers(self.state, self.w.d, self.c, self.w.h)

    def test_emission_not_fully_posted(self):
        self.post(self.c.start + 20)
        self.w.advance_to(self.c.end + self.c.claim_window + 1)
        self.assertTrue(any("posted only up to" in b for b in self.blockers()))

    def test_final_root_still_pending(self):
        self.post(self.c.end)
        self.assertTrue(any("still pending" in b for b in self.blockers()))

    def test_the_near_miss(self):
        """The final root became claimable a few blocks ago and nobody has claimed: refused."""
        self.post(self.c.end)
        act = self.state["roots"]["1"]["activates_at"]
        self.w.advance_to(act + 7)
        reasons = self.blockers()
        self.assertTrue(any("unclaimed in the final root" in b for b in reasons), reasons)

    def test_after_claims_or_a_full_window_it_is_allowed(self):
        root = self.post(self.c.end)
        act = self.state["roots"]["1"]["activates_at"]
        self.w.advance_to(act + 1)
        self.w.claim(root, self.c.id, "alice")
        self.w.claim(root, self.c.id, "bob")
        self.assertEqual(self.blockers(), [])

    def test_unclaimed_after_a_full_window_goes_to_the_creator(self):
        """By design: a holder who never claims within the window forfeits."""
        root = self.post(self.c.end)
        act = self.state["roots"]["1"]["activates_at"]
        self.w.advance_to(act + 1)
        self.w.claim(root, self.c.id, "alice")
        self.w.advance_to(act + self.c.claim_window)
        self.assertEqual(self.blockers(), [])


if __name__ == "__main__":
    unittest.main()
