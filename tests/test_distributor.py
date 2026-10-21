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
from flpcr.distributor import HEIGHT_WINDOW

DIST = b"\xd1" * 32
POOL = b"\xb0" * 32
LP = b"\x1b" * 32
REWARD = b"\xca" * 32
ALICE, BOB = b"\xa1" * 32, b"\xb2" * 32
CREATOR, DEV, ADMIN, GUARDIAN, ENGINE, ROUTER, STRANGER = (
    bytes([x]) * 32 for x in (0xCC, 0x7E, 0xAD, 0x6A, 0xE6, 0x80, 0x55)
)
HOT = b"\x40" * 32

DISPUTE = 10
ACTIVE_AFTER = HEIGHT_WINDOW + DISPUTE  # a root posted at h is claimable from h + ACTIVE_AFTER
WINDOW = 1000


def config(**kw):
    kw.setdefault("dispute_blocks", DISPUTE)
    return Config(DIST, REWARD, DEV, ADMIN, GUARDIAN, ROUTER, **kw)


def setup(deposit=1_000_000, start=100, end=200):
    d = Distributor(config(), ENGINE)
    c = Campaign(DIST, CREATOR, REWARD, deposit, start, end, Kind.LP_HOLD, POOL, claim_window=WINDOW)
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, ALICE, 3), HolderEvent(0, BOB, 1)]})
    d.create(50, ROUTER, c, deposit, CREATOR)
    return d, c, src


def post(d, root, h):
    d.post_root(h, ENGINE, root.root_id, root.start, root.end, root.root, root.totals)


def claim(d, root, h, ph, campaign):
    cl = root.claim_for(campaign.id, ph)
    return d.claim(h, campaign.id, ph, cl.cumulative, cl.proof, ph)


class TestLifecycle(unittest.TestCase):
    def test_fee_claims_over_time_and_reclaim(self):
        d, c, src = setup()
        self.assertEqual((d.dev_fee_paid, d.reserve), (30_000, 970_000))  # 3%, paid at creation
        d.check_invariant()

        # Mid-campaign root: Alice claims what she has so far.
        state, r1 = run_epoch(EngineState(DIST, height=100), [c], src, 150)
        post(d, r1, 150)
        with self.assertRaises(Refused):
            claim(d, r1, 150 + ACTIVE_AFTER - 1, ALICE, c)  # still in the dispute window
        self.assertEqual(claim(d, r1, 150 + ACTIVE_AFTER, ALICE, c), (ALICE, 363_750))
        with self.assertRaises(Refused):
            claim(d, r1, 150 + ACTIVE_AFTER + 1, ALICE, c)  # nothing new until the next root

        # Final root: Alice claims only the difference; Bob never claims.
        state, r2 = run_epoch(state, [c], src, 200)
        post(d, r2, 200)
        self.assertEqual(claim(d, r2, 200 + ACTIVE_AFTER, ALICE, c), (ALICE, 727_500 - 363_750))
        d.check_invariant()

        # The creator gets Bob's unclaimed 242,500 back, but not before the window passes.
        with self.assertRaises(Refused):
            d.reclaim(200 + WINDOW - 1, c.id, CREATOR)
        self.assertEqual(d.reclaim(200 + WINDOW, c.id, CREATOR), (CREATOR, 242_500))
        with self.assertRaises(Refused):
            claim(d, r2, 200 + WINDOW, BOB, c)  # reclaimed: nothing left to claim from
        self.assertEqual(d.reserve, 0)
        d.check_invariant()

    def test_holders_claim_past_the_window_until_the_creator_reclaims(self):
        # Twice the window has passed and the creator has not reclaimed: Bob still claims.
        d, c, src = setup()
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 200)
        post(d, r, 200)
        late = 200 + 2 * WINDOW
        self.assertEqual(claim(d, r, late, BOB, c), (BOB, 242_500))
        self.assertEqual(d.reclaim(late, c.id, CREATOR), (CREATOR, 727_500))  # only Alice's share was left
        with self.assertRaises(Refused):
            claim(d, r, late, ALICE, c)
        d.check_invariant()

    def test_claim_window_is_the_creators_and_bounded(self):
        d, c, src = setup()
        self.assertEqual(c.reclaim_from, 200 + WINDOW)
        floor = 3 * DISPUTE + HEIGHT_WINDOW
        self.assertEqual(d.config.min_claim_window, floor)
        short = Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, nonce=1, claim_window=floor - 1)
        with self.assertRaises(Refused):
            d.create(50, ROUTER, short, 1000, CREATOR)
        ok = Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, nonce=1, claim_window=floor)
        d.create(50, ROUTER, ok, 1000, CREATOR)
        other = Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, nonce=1, claim_window=WINDOW + 1)
        self.assertNotEqual(other.id, Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, nonce=1,
                                               claim_window=WINDOW).id)

    def test_unearned_budget_also_goes_back(self):
        # Nobody holds LP for the first half: that half was never earned.
        d = Distributor(config(fee_bps=0), ENGINE)
        c = Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, fee_bps=0, claim_window=WINDOW)
        src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(150, ALICE, 1)]})
        d.create(50, ROUTER, c, 1000, CREATOR)
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 200)
        post(d, r, 200)
        self.assertEqual(claim(d, r, 200 + ACTIVE_AFTER, ALICE, c), (ALICE, 500))
        self.assertEqual(d.reclaim(200 + WINDOW, c.id, CREATOR), (CREATOR, 500))


class TestConfig(unittest.TestCase):
    def test_testnet_allows_minute_scale_campaigns(self):
        cfg = Config.testnet(DIST, REWARD, DEV, ADMIN, GUARDIAN, ROUTER)
        self.assertEqual((cfg.dispute_blocks, cfg.min_campaign_length), (8, 16))  # ~2.5 min, ~5 min
        self.assertEqual(cfg.min_claim_window, 56)
        d = Distributor(cfg, ENGINE)
        d.create(50, ROUTER, Campaign(DIST, CREATOR, REWARD, 1000, 100, 116, Kind.LP_HOLD, POOL, claim_window=56),
                 1000, CREATOR)
        with self.assertRaises(Refused):
            d.create(50, ROUTER, Campaign(DIST, CREATOR, REWARD, 1000, 100, 115, Kind.LP_HOLD, POOL, claim_window=56),
                     1000, CREATOR)

    def test_production_defaults(self):
        cfg = Config(DIST, REWARD, DEV, ADMIN, GUARDIAN, ROUTER)
        self.assertEqual((cfg.fee_bps, cfg.dispute_blocks, cfg.min_campaign_length), (300, 38, 77))
        self.assertEqual(cfg.max_campaign_length, 365 * 4608)
        self.assertEqual(cfg.min_claim_window, 3 * 38 + 32)
        self.assertGreater(cfg.rotate_delay, cfg.dispute_blocks)
        d = Distributor(cfg, ENGINE)
        win = cfg.min_claim_window
        with self.assertRaises(Refused):
            d.create(50, ROUTER, Campaign(DIST, CREATOR, REWARD, 1000, 100, 100 + 365 * 4608 + 1, Kind.LP_HOLD, POOL,
                                          claim_window=win), 1000, CREATOR)
        d.create(50, ROUTER, Campaign(DIST, CREATOR, REWARD, 1000, 100, 177, Kind.LP_HOLD, POOL, claim_window=win),
                 1000, CREATOR)

    def test_config_is_checked(self):
        with self.assertRaises(ValueError):
            config(rotate_delay=DISPUTE)  # a rotation must outlast a dispute window
        with self.assertRaises(ValueError):
            config(delegate_fee=-1)
        with self.assertRaises(ValueError):
            Config(DIST, REWARD, DEV, b"\xad" * 31, GUARDIAN, ROUTER)

    def test_claims_at_any_interval_within_the_campaign(self):
        # Roots every 10 blocks; Alice claims after each one, Bob only once at the end.
        cfg = Config.testnet(DIST, REWARD, DEV, ADMIN, GUARDIAN, ROUTER, fee_bps=0)
        after = HEIGHT_WINDOW + cfg.dispute_blocks
        d = Distributor(cfg, ENGINE)
        c = Campaign(DIST, CREATOR, REWARD, 1000, 100, 150, Kind.LP_HOLD, POOL, fee_bps=0, claim_window=56)
        src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, ALICE, 1), HolderEvent(0, BOB, 1)]})
        d.create(50, ROUTER, c, 1000, CREATOR)
        state, alice, h = EngineState(DIST, height=100), 0, 100
        for end in range(110, 151, 10):
            state, root = run_epoch(state, [c], src, end)
            h = max(h, end)
            post(d, root, h)
            h += after
            alice += claim(d, root, h, ALICE, c)[1]
        self.assertEqual(alice, 500)
        self.assertEqual(claim(d, root, h, BOB, c), (BOB, 500))
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
        h = 150 + ACTIVE_AFTER
        with self.assertRaises(Refused):
            d.post_root(h, ENGINE, 2, 151, 180, r2.root, r2.totals)
        with self.assertRaises(Refused):
            d.post_root(h, ENGINE, 3, 150, 180, r2.root, r2.totals)
        with self.assertRaises(Refused):
            d.post_root(h, ENGINE, 2, 150, 180, r2.root, {c.id: r1.totals[c.id] - 1})
        post(d, r2, h)

    def test_guardian_veto(self):
        d, c, src = setup()
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 150)
        post(d, r, 150)
        with self.assertRaises(Refused):
            d.veto(151, STRANGER, 1)
        d.veto(151, GUARDIAN, 1)
        with self.assertRaises(Refused):
            claim(d, r, 150 + ACTIVE_AFTER, ALICE, c)  # never became active
        post(d, r, 152)  # the corrected root reuses the id
        with self.assertRaises(Refused):
            d.veto(152 + ACTIVE_AFTER, GUARDIAN, 1)  # too late once active

    def test_forged_and_foreign_proofs(self):
        d, c, src = setup()
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 200)
        post(d, r, 200)
        cl = r.claim_for(c.id, BOB)
        h = 200 + ACTIVE_AFTER
        with self.assertRaises(Refused):
            d.claim(h, c.id, BOB, cl.cumulative + 1, cl.proof, BOB)
        with self.assertRaises(Refused):
            d.claim(h, c.id, STRANGER, cl.cumulative, cl.proof, STRANGER)

    def test_only_the_holder_claims_and_only_by_spending(self):
        d, c, src = setup()
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 200)
        post(d, r, 200)
        cl = r.claim_for(c.id, BOB)
        h = 200 + ACTIVE_AFTER
        with self.assertRaises(Refused):
            d.claim(h, c.id, BOB, cl.cumulative, cl.proof, STRANGER)  # a keeper, or anyone else
        with self.assertRaises(Refused):
            d.claim(h, c.id, BOB, cl.cumulative, cl.proof, BOB, by_spend=False)  # a bare signature
        self.assertEqual(d.claim(h, c.id, BOB, cl.cumulative, cl.proof, BOB), (BOB, 242_500))

    def test_delegate_claims_for_holder(self):
        d, c, src = setup()
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 200)
        post(d, r, 200)
        cl = r.claim_for(c.id, BOB)
        h = 200 + ACTIVE_AFTER
        with self.assertRaises(Refused):
            d.claim(h, c.id, BOB, cl.cumulative, cl.proof, HOT)
        with self.assertRaises(Refused):
            d.set_delegate(h, BOB, HOT, True, HOT)  # the delegate cannot grant itself
        with self.assertRaises(Refused):
            d.set_delegate(h, BOB, HOT, False, BOB)  # nothing to revoke yet
        d.set_delegate(h, BOB, HOT, True, BOB)
        self.assertEqual(d.claim(h, c.id, BOB, cl.cumulative, cl.proof, HOT), (BOB, 242_500))
        d.set_delegate(h + 1, BOB, HOT, False, BOB)
        d.set_delegate(h + 1, BOB, HOT, False, BOB)  # the key still exists: an inactive update

    def test_grant_twice_revoke_once_revokes(self):
        d, c, src = setup()
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 200)
        post(d, r, 200)
        cl = r.claim_for(c.id, BOB)
        h = 200 + ACTIVE_AFTER
        d.set_delegate(h, BOB, HOT, True, BOB)
        d.set_delegate(h, BOB, HOT, True, BOB)
        d.set_delegate(h, BOB, HOT, False, BOB)
        with self.assertRaises(Refused):
            d.claim(h, c.id, BOB, cl.cumulative, cl.proof, HOT)

    def test_who_reclaims(self):
        d, c, src = setup()
        with self.assertRaises(Refused):
            d.reclaim(200 + WINDOW, c.id, STRANGER)
        with self.assertRaises(Refused):
            d.reclaim(200 + WINDOW, c.id, HOT)
        with self.assertRaises(Refused):
            d.reclaim(200 + WINDOW, c.id, STRANGER, as_admin=True)
        with self.assertRaises(Refused):
            d.reclaim(200 + WINDOW - 1, c.id, ADMIN, as_admin=True)  # the admin waits for the window too
        d.set_delegate(150, CREATOR, HOT, True, CREATOR)
        self.assertEqual(d.reclaim(200 + WINDOW, c.id, HOT), (CREATOR, 970_000))

    def test_admin_reclaim_pays_the_creator(self):
        d, c, src = setup()
        self.assertEqual(d.reclaim(200 + WINDOW, c.id, ADMIN, as_admin=True), (CREATOR, 970_000))
        d.check_invariant()

    def test_rotation_waits_and_admin_cancels(self):
        d, c, src = setup()
        cfg = d.config
        NEW = b"\xe7" * 32
        with self.assertRaises(Refused):
            d.rotate(60, STRANGER, NEW)
        d.rotate(60, GUARDIAN, NEW)
        at = 60 + HEIGHT_WINDOW + cfg.rotate_delay
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 150)
        with self.assertRaises(Refused):
            d.post_root(150, NEW, r.root_id, r.start, r.end, r.root, r.totals)  # not yet
        with self.assertRaises(Refused):
            d.cancel_rotation(150, STRANGER, NEW)
        with self.assertRaises(Refused):
            d.cancel_rotation(150, ADMIN, b"\xe8" * 32)
        d.cancel_rotation(150, ADMIN, NEW)
        with self.assertRaises(Refused):
            d.post_root(at, NEW, r.root_id, r.start, r.end, r.root, r.totals)  # cancelled
        d.rotate(at, GUARDIAN, NEW)
        later = at + HEIGHT_WINDOW + cfg.rotate_delay
        d.post_root(later, NEW, r.root_id, r.start, r.end, r.root, r.totals)
        self.assertEqual(d.engine_ph, NEW)

    def test_dispute_and_resolve(self):
        d, c, src = setup()
        _, r = run_epoch(EngineState(DIST, height=100), [c], src, 150)
        post(d, r, 150)
        bond = d.config.dispute_bond
        with self.assertRaises(Refused):
            d.dispute_root(151, 2, STRANGER, bond, b"\x01" * 32)  # not the pending root
        with self.assertRaises(Refused):
            d.dispute_root(151, 1, STRANGER, bond - 1, b"\x01" * 32)  # short bond
        d.dispute_root(151, 1, STRANGER, bond, b"\x01" * 32)
        with self.assertRaises(Refused):
            d.dispute_root(152, 1, HOT, bond, b"\x02" * 32)  # one at a time
        with self.assertRaises(Refused):
            d.veto(152, GUARDIAN, 1)  # settled by resolve, not veto
        with self.assertRaises(Refused):
            claim(d, r, 150 + ACTIVE_AFTER, ALICE, c)  # a disputed root does not activate
        with self.assertRaises(Refused):
            d.resolve(160, STRANGER, True)
        self.assertEqual(d.resolve(160, ADMIN, False), (DEV, bond))  # rejected: forfeited, window restarts
        self.assertEqual(d.pending.activates_at, 160 + ACTIVE_AFTER)
        with self.assertRaises(Refused):
            claim(d, r, 160 + ACTIVE_AFTER - 1, ALICE, c)
        d.dispute_root(161, 1, STRANGER, bond, b"\x03" * 32)
        self.assertEqual(d.resolve(162, GUARDIAN, True), (STRANGER, bond))  # upheld: revoked, refunded
        self.assertIsNone(d.pending)
        post(d, r, 163)  # the corrected root reuses the id
        self.assertEqual(claim(d, r, 163 + ACTIVE_AFTER, ALICE, c), (ALICE, 363_750))

    def test_create_rules(self):
        d = Distributor(config(), ENGINE)
        c = Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, claim_window=WINDOW)
        with self.assertRaises(Refused):
            d.create(100 - HEIGHT_WINDOW + 1, ROUTER, c, 1000, CREATOR)  # starts inside the HEIGHT_WINDOW slack
        with self.assertRaises(Refused):
            d.create(50, ROUTER, c, 999, CREATOR)
        with self.assertRaises(Refused):
            d.create(50, ROUTER, Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, fee_bps=0,
                                          claim_window=WINDOW), 1000, CREATOR)
        with self.assertRaises(Refused):
            d.create(50, STRANGER, c, 1000, CREATOR)  # not our router
        with self.assertRaises(Refused):
            d.create(50, ROUTER, c, 1000, STRANGER)  # someone else consents for the creator
        far = Campaign(DIST, CREATOR, REWARD, 1000, 50 + d.config.max_campaign_length + 1,
                       50 + d.config.max_campaign_length + 100, Kind.LP_HOLD, POOL, claim_window=WINDOW)
        with self.assertRaises(Refused):
            d.create(50, ROUTER, far, 1000, CREATOR)  # starts further ahead than any campaign runs
        d.create(100 - HEIGHT_WINDOW, ROUTER, c, 1005, CREATOR)  # slack is network fee, as on chain
        self.assertEqual((d.dev_fee_paid, d.reserve), (30, 970))
        with self.assertRaises(Refused):
            d.create(50, ROUTER, c, 1000, CREATOR)
        d.check_invariant()


if __name__ == "__main__":
    unittest.main()
