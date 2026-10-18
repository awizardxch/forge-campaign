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
CREATOR, TREASURY, GUARDIAN, ENGINE, ROUTER, STRANGER = (bytes([x]) * 32 for x in (0xCC, 0x7E, 0x6A, 0xE6, 0x80, 0x55))

DISPUTE = 10
WINDOW = 1000


def setup(deposit=1_000_000, start=100, end=200):
    cfg = Config(DIST, REWARD, TREASURY, GUARDIAN, ROUTER, dispute_blocks=DISPUTE)
    d = Distributor(cfg, ENGINE)
    c = Campaign(DIST, CREATOR, REWARD, deposit, start, end, Kind.LP_HOLD, POOL, claim_window=WINDOW)
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, ALICE, 3), HolderEvent(0, BOB, 1)]})
    d.create(50, ROUTER, c, deposit)
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

        # The creator gets Bob's unclaimed 242,500 back, but not before the window passes.
        with self.assertRaises(Refused):
            d.reclaim(200 + WINDOW - 1, c.id)
        self.assertEqual(d.reclaim(200 + WINDOW, c.id), (CREATOR, 242_500))
        with self.assertRaises(Refused):
            claim(d, r2, 200 + WINDOW, BOB, c)  # reclaimed: nothing left to claim from
        self.assertEqual(d.collect(200 + WINDOW), (TREASURY, 30_000))
        self.assertEqual(d.reserve, 0)
        d.check_invariant()

    def test_holders_claim_past_the_window_until_the_creator_reclaims(self):
        # Twice the window has passed and the creator has not reclaimed: Bob still claims.
        d, c, src = setup()
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 200)
        post(d, r, 200)
        late = 200 + 2 * WINDOW
        self.assertEqual(claim(d, r, late, BOB, c), (BOB, 242_500))
        self.assertEqual(d.reclaim(late, c.id), (CREATOR, 727_500))  # only Alice's share was left
        with self.assertRaises(Refused):
            claim(d, r, late, ALICE, c)
        d.check_invariant()

    def test_claim_window_is_the_creators_and_bounded(self):
        d, c, src = setup()
        self.assertEqual(c.reclaim_from, 200 + WINDOW)
        short = Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, nonce=1, claim_window=2 * DISPUTE - 1)
        with self.assertRaises(Refused):
            d.create(50, ROUTER, short, 1000)
        other = Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, nonce=1, claim_window=WINDOW + 1)
        self.assertNotEqual(other.id, Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, nonce=1, claim_window=WINDOW).id)

    def test_unearned_budget_also_goes_back(self):
        # Nobody holds LP for the first half: that half was never earned.
        cfg = Config(DIST, REWARD, TREASURY, GUARDIAN, ROUTER, fee_bps=0, dispute_blocks=DISPUTE)
        d = Distributor(cfg, ENGINE)
        c = Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, fee_bps=0, claim_window=WINDOW)
        src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(150, ALICE, 1)]})
        d.create(50, ROUTER, c, 1000)
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 200)
        post(d, r, 200)
        self.assertEqual(claim(d, r, 210, ALICE, c), (ALICE, 500))
        self.assertEqual(d.reclaim(200 + WINDOW, c.id), (CREATOR, 500))


class TestConfig(unittest.TestCase):
    def test_testnet_allows_minute_scale_campaigns(self):
        cfg = Config.testnet(DIST, REWARD, TREASURY, GUARDIAN, ROUTER)
        self.assertEqual((cfg.dispute_blocks, cfg.min_campaign_length), (8, 16))  # ~2.5 min, ~5 min
        d = Distributor(cfg, ENGINE)
        d.create(50, ROUTER, Campaign(DIST, CREATOR, REWARD, 1000, 100, 116, Kind.LP_HOLD, POOL, claim_window=16), 1000)
        with self.assertRaises(Refused):
            d.create(50, ROUTER, Campaign(DIST, CREATOR, REWARD, 1000, 100, 115, Kind.LP_HOLD, POOL, claim_window=16), 1000)

    def test_production_defaults(self):
        cfg = Config(DIST, REWARD, TREASURY, GUARDIAN, ROUTER)
        self.assertEqual((cfg.fee_bps, cfg.dispute_blocks, cfg.min_campaign_length), (300, 38, 77))
        self.assertEqual(cfg.max_campaign_length, 365 * 4608)
        self.assertEqual(cfg.min_claim_window, 76)
        d = Distributor(cfg, ENGINE)
        with self.assertRaises(Refused):
            d.create(50, ROUTER, Campaign(DIST, CREATOR, REWARD, 1000, 100, 100 + 365 * 4608 + 1, Kind.LP_HOLD, POOL), 1000)
        d.create(50, ROUTER, Campaign(DIST, CREATOR, REWARD, 1000, 100, 177, Kind.LP_HOLD, POOL), 1000)

    def test_claims_at_any_interval_within_the_campaign(self):
        # Roots every 10 blocks; Alice claims after each one, Bob only once at the end.
        cfg = Config.testnet(DIST, REWARD, TREASURY, GUARDIAN, ROUTER, fee_bps=0)
        d = Distributor(cfg, ENGINE)
        c = Campaign(DIST, CREATOR, REWARD, 1000, 100, 150, Kind.LP_HOLD, POOL, fee_bps=0, claim_window=16)
        src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, ALICE, 1), HolderEvent(0, BOB, 1)]})
        d.create(50, ROUTER, c, 1000)
        state, alice = EngineState(DIST, height=100), 0
        for end in range(110, 151, 10):
            state, root = run_epoch(state, [c], src, end)
            post(d, root, end + cfg.dispute_blocks)
            alice += claim(d, root, end + 2 * cfg.dispute_blocks, ALICE, c)[1]
        self.assertEqual(alice, 500)
        self.assertEqual(claim(d, root, 200, BOB, c), (BOB, 500))
        d.check_invariant()


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
        cfg = Config(DIST, REWARD, TREASURY, GUARDIAN, ROUTER)
        d = Distributor(cfg, ENGINE)
        c = Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL)
        with self.assertRaises(Refused):
            d.create(101, ROUTER, c, 1000)  # starts in the past
        with self.assertRaises(Refused):
            d.create(50, ROUTER, c, 999)
        with self.assertRaises(Refused):
            d.create(50, ROUTER, Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, fee_bps=0), 1000)
        with self.assertRaises(Refused):
            d.create(50, STRANGER, c, 1000)  # not our router
        d.create(50, ROUTER, c, 1005)  # slack is network fee, as on chain
        self.assertEqual((d.fees_owed, d.reserve), (30, 1000))
        with self.assertRaises(Refused):
            d.create(50, ROUTER, c, 1000)
        d.check_invariant()


if __name__ == "__main__":
    unittest.main()
