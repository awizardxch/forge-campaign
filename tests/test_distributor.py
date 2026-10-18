import unittest

from flpcr import (
    Campaign,
    Config,
    Distributor,
    EngineState,
    FixtureSource,
    HolderEvent,
    Kind,
    Refused,
    run_epoch,
)

DIST = b"\xd1" * 32
POOL = b"\xb0" * 32
LP = b"\x1b" * 32
REWARD = b"\xca" * 32
ALICE, BOB = b"\xa1" * 32, b"\xb2" * 32
CREATOR, TREASURY, GUARDIAN, ENGINE, STRANGER = (bytes([x]) * 32 for x in (0xCC, 0x7E, 0x6A, 0xE6, 0x55))

DISPUTE = 10
WINDOW = 1000


def setup(deposit=1_000_000, start=100, end=200):
    cfg = Config(DIST, REWARD, TREASURY, GUARDIAN, dispute_blocks=DISPUTE, claim_window=WINDOW)
    d = Distributor(cfg, ENGINE)
    c = Campaign(DIST, CREATOR, REWARD, deposit, start, end, Kind.LP_HOLD, POOL)
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, ALICE, 3), HolderEvent(0, BOB, 1)]})
    d.create(50, c, deposit)
    return d, c, src


def post(d, root, h):
    d.post_root(h, ENGINE, root.root_id, root.start, root.end, root.root, root.totals)


def claim(d, root, h, ph, campaign):
    cl = root.claim_for(campaign.id, ph)
    return d.claim(h, campaign.id, ph, cl.cumulative, cl.proof)


class TestLifecycle(unittest.TestCase):
    def test_fee_claims_over_time_and_reclaim(self):
        d, c, src = setup()
        self.assertEqual(d.fees_owed, 30_000)  # 3%
        d.check_invariant()

        # Mid-campaign root: Alice claims what she has so far.
        state, r1 = run_epoch(EngineState(DIST, height=100), [c], src, 150)
        post(d, r1, 150)
        with self.assertRaises(Refused):
            claim(d, r1, 155, ALICE, c)  # still in the dispute window
        self.assertEqual(claim(d, r1, 160, ALICE, c), (ALICE, 363_750))
        with self.assertRaises(Refused):
            claim(d, r1, 161, ALICE, c)  # nothing new until the next root

        # Final root: Alice claims only the difference; Bob never claims.
        state, r2 = run_epoch(state, [c], src, 200)
        post(d, r2, 200)
        self.assertEqual(claim(d, r2, 210, ALICE, c), (ALICE, 727_500 - 363_750))
        d.check_invariant()

        # Creator gets Bob's unclaimed 242,500 back, but not before the window closes.
        with self.assertRaises(Refused):
            d.reclaim(200 + WINDOW - 1, c.id)
        self.assertEqual(d.reclaim(200 + WINDOW, c.id), (CREATOR, 242_500))
        with self.assertRaises(Refused):
            claim(d, r2, 200 + WINDOW, BOB, c)
        self.assertEqual(d.collect(200 + WINDOW), (TREASURY, 30_000))
        self.assertEqual(d.reserve, 0)
        d.check_invariant()

    def test_claim_window_closes_for_holders(self):
        d, c, src = setup()
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 200)
        post(d, r, 200)
        with self.assertRaises(Refused):
            claim(d, r, 200 + WINDOW, BOB, c)

    def test_unearned_budget_also_goes_back(self):
        # Nobody holds LP for the first half: that half was never earned.
        cfg = Config(DIST, REWARD, TREASURY, GUARDIAN, fee_bps=0, dispute_blocks=DISPUTE, claim_window=WINDOW)
        d = Distributor(cfg, ENGINE)
        c = Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, fee_bps=0)
        src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(150, ALICE, 1)]})
        d.create(50, c, 1000)
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 200)
        post(d, r, 200)
        self.assertEqual(claim(d, r, 210, ALICE, c), (ALICE, 500))
        self.assertEqual(d.reclaim(200 + WINDOW, c.id), (CREATOR, 500))


class TestRefusals(unittest.TestCase):
    def test_only_engine_posts_and_ceiling_holds(self):
        d, c, src = setup()
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 150)
        with self.assertRaises(Refused):
            d.post_root(150, STRANGER, 1, 100, 150, r.root, r.totals)
        with self.assertRaises(Refused):
            d.post_root(150, ENGINE, 1, 100, 150, r.root, {c.id: c.emittable(150) + 1})
        with self.assertRaises(Refused):
            d.post_root(149, ENGINE, 1, 100, 150, r.root, r.totals)  # window ends in the future
        post(d, r, 150)
        with self.assertRaises(Refused):
            d.post_root(155, ENGINE, 2, 150, 155, r.root, r.totals)  # first still in dispute

    def test_roots_tile_and_totals_never_fall(self):
        d, c, src = setup()
        state, r1 = run_epoch(EngineState(DIST, height=100), [c], src, 150)
        post(d, r1, 150)
        _, r2 = run_epoch(state, [c], src, 180)
        with self.assertRaises(Refused):
            d.post_root(190, ENGINE, 2, 151, 180, r2.root, r2.totals)
        with self.assertRaises(Refused):
            d.post_root(190, ENGINE, 3, 150, 180, r2.root, r2.totals)
        with self.assertRaises(Refused):
            d.post_root(190, ENGINE, 2, 150, 180, r2.root, {c.id: r1.totals[c.id] - 1})
        post(d, r2, 190)

    def test_guardian_veto(self):
        d, c, src = setup()
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 150)
        post(d, r, 150)
        with self.assertRaises(Refused):
            d.veto(151, STRANGER, 1)
        d.veto(151, GUARDIAN, 1)
        with self.assertRaises(Refused):
            claim(d, r, 170, ALICE, c)  # never became active
        post(d, r, 152)  # the corrected root reuses the id
        with self.assertRaises(Refused):
            d.veto(152 + DISPUTE, GUARDIAN, 1)  # too late once active

    def test_forged_and_foreign_proofs(self):
        d, c, src = setup()
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 200)
        post(d, r, 200)
        cl = r.claim_for(c.id, BOB)
        with self.assertRaises(Refused):
            d.claim(210, c.id, BOB, cl.cumulative + 1, cl.proof)
        with self.assertRaises(Refused):
            d.claim(210, c.id, STRANGER, cl.cumulative, cl.proof)

    def test_create_rules(self):
        cfg = Config(DIST, REWARD, TREASURY, GUARDIAN)
        d = Distributor(cfg, ENGINE)
        c = Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL)
        with self.assertRaises(Refused):
            d.create(101, c, 1000)  # starts in the past
        with self.assertRaises(Refused):
            d.create(50, c, 999)
        with self.assertRaises(Refused):
            d.create(50, Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, fee_bps=0), 1000)
        d.create(50, c, 1005)  # slack is kept as fee
        self.assertEqual(d.fees_owed, 30 + 5)
        with self.assertRaises(Refused):
            d.create(50, c, 1000)
        d.check_invariant()


if __name__ == "__main__":
    unittest.main()
