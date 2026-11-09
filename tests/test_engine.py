import random
import unittest
from fractions import Fraction

from flpcr import (
    Campaign,
    EngineState,
    FixtureSource,
    HolderEvent,
    Kind,
    Params,
    accrue,
    root_from_proof,
    run_epoch,
)
from flpcr.engine import Holdings, Requirements, Weights, fee_buckets
from flpcr.manifest import root_to_json, state_from_json, state_to_json
from flpcr.model import BLOCKS_PER_DAY, MIN_FEE_WINDOW, daily_emission

DIST = b"\xd1" * 32
POOL = b"\xb0" * 32
LP = b"\x1b" * 32
VAULT_LP = b"\x2b" * 32
REWARD = b"\xca" * 32
ALICE, BOB, CAROL, DAVE = (bytes([x]) * 32 for x in (0xA1, 0xB2, 0xC3, 0xD4))
RESERVE = b"\xee" * 32
VAULT = b"\x7a" * 32
CREATOR = b"\xcc" * 32


def lp_campaign(amount=1000, start=100, end=200, params=Params(), nonce=0, fee_bps=0):
    # fee_bps=0 keeps the scoring arithmetic readable; the fee has its own test.
    return Campaign(DIST, CREATOR, REWARD, amount, start, end, Kind.LP_HOLD, POOL, params, nonce, fee_bps)


def fresh(height=100):
    """Engine state for DIST with accrual complete up to `height`."""
    return EngineState(DIST, height=height)


def source(events, fees=(), wrappers=None, exclusions=None):
    return FixtureSource(
        lp_assets={POOL: LP},
        events={LP: list(events), VAULT_LP: list((wrappers or {}).get("events", []))},
        fees={POOL: list(fees)},
        exclusions=exclusions or {LP: {RESERVE}},
        wrappers={LP: {VAULT: VAULT_LP}} if wrappers else {},
    )


class TestCampaign(unittest.TestCase):
    def test_three_percent_fee_comes_out_of_the_deposit(self):
        c = Campaign(DIST, CREATOR, REWARD, 1_000_000, 0, 10, Kind.LP_HOLD, POOL)
        self.assertEqual(c.fee_bps, 300)
        self.assertEqual((c.fee, c.budget), (30_000, 970_000))
        self.assertEqual(c.emittable(10), 970_000)
        self.assertNotEqual(c.id, lp_campaign(1_000_000, 0, 10).id)

    def test_claim_window_is_part_of_the_terms(self):
        a = lp_campaign()
        b = Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, fee_bps=0, claim_window=4608)
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(b.reclaim_from, 200 + 4608)
        with self.assertRaises(ValueError):
            Campaign(DIST, CREATOR, REWARD, 1000, 100, 200, Kind.LP_HOLD, POOL, claim_window=0)

    def test_emittable_telescopes_to_amount(self):
        c = lp_campaign(amount=1001, start=0, end=7)
        self.assertEqual(c.emittable(7), 1001)
        self.assertEqual(c.emittable(-5), 0)
        self.assertEqual(c.emittable(99), 1001)
        steps = [c.emittable(h + 1) - c.emittable(h) for h in range(7)]
        self.assertEqual(sum(steps), 1001)

    def test_id_commits_to_every_field(self):
        base = lp_campaign()
        variants = [
            lp_campaign(amount=1001),
            lp_campaign(end=201),
            lp_campaign(nonce=1),
            lp_campaign(params=Params(min_balance=1)),
            lp_campaign(params=Params(fee_window=BLOCKS_PER_DAY + 1)),
        ]
        for v in variants:
            self.assertNotEqual(base.id, v.id)

    def test_validation(self):
        with self.assertRaises(ValueError):
            lp_campaign(params=Params(time_bps=5000, fees_bps=4000))
        with self.assertRaises(ValueError):
            Campaign(DIST, CREATOR, REWARD, 10, 0, 10, Kind.CAT_HOLD, LP, Params(time_bps=0, fees_bps=10000), fee_bps=0)
        with self.assertRaises(ValueError):
            lp_campaign(end=100)
        for bad in (0, -1, 1.5, True, MIN_FEE_WINDOW - 1):
            with self.assertRaises(ValueError):
                Params(fee_window=bad)
        self.assertEqual(Params(fee_window=MIN_FEE_WINDOW).fee_window, 192)
        self.assertEqual(Params().fee_window, BLOCKS_PER_DAY)


class TestAccrual(unittest.TestCase):
    def test_equal_holders_split_evenly(self):
        src = source([HolderEvent(0, ALICE, 50), HolderEvent(0, BOB, 50)])
        credit = accrue(lp_campaign(), src, 100, 200)
        self.assertEqual(credit, {ALICE: 500, BOB: 500})

    def test_share_of_supply_per_block(self):
        # Alice alone for the first half; Bob joins with 3x her size for the second.
        src = source([HolderEvent(0, ALICE, 10), HolderEvent(150, BOB, 30)])
        credit = accrue(lp_campaign(), src, 100, 200)
        self.assertEqual(credit[ALICE], 500 + Fraction(500, 4))
        self.assertEqual(credit[BOB], Fraction(500 * 3, 4))

    def test_reserve_and_blacklist_excluded(self):
        src = source([HolderEvent(0, ALICE, 10), HolderEvent(0, RESERVE, 1000), HolderEvent(0, BOB, 10)])
        credit = accrue(lp_campaign(params=Params(blacklist=frozenset({BOB}))), src, 100, 200)
        self.assertEqual(credit, {ALICE: 1000})

    def test_min_balance(self):
        src = source([HolderEvent(0, ALICE, 10), HolderEvent(0, BOB, 9)])
        credit = accrue(lp_campaign(params=Params(min_balance=10)), src, 100, 200)
        self.assertEqual(credit, {ALICE: 1000})

    def test_nobody_eligible_leaves_budget_unallocated(self):
        src = source([HolderEvent(150, ALICE, 10)])
        credit = accrue(lp_campaign(), src, 100, 200)
        self.assertEqual(credit, {ALICE: 500})

    def test_fee_share_is_averaged_over_the_lookback(self):
        # Alice holds throughout; Bob is in only for [600, 800). Every fee is paid at 640.
        src = source(
            [HolderEvent(0, ALICE, 10), HolderEvent(600, BOB, 10)],
            fees=[(640, 7)],
        )
        params = Params(time_bps=5000, fees_bps=5000, fee_window=200)
        credit = accrue(lp_campaign(start=400, end=800, params=params), src, 400, 800)
        # time half: Alice 250 + 125, Bob 125.
        # fee bucket [400, 600): no swap, so time weighted: Alice 250.
        # fee bucket [600, 800): 250 for the swap at 640, by the share averaged
        # over [440, 640), the 200 blocks before it: Alice alone for 160, half for 40.
        self.assertEqual(credit[ALICE], 375 + 250 + Fraction(250 * 180, 200))
        self.assertEqual(credit[BOB], 125 + Fraction(250 * 20, 200))

    def test_fee_share_waits_for_its_bucket_to_close(self):
        src = source([HolderEvent(0, ALICE, 10)], fees=[(550, 1)])
        c = lp_campaign(start=500, end=1000, params=Params(time_bps=5000, fees_bps=5000, fee_window=200))
        # Buckets [500,700), [700,900), [900,1000). A window to 750 holds the
        # first bucket's fee share, not the second's.
        self.assertEqual(accrue(c, src, 500, 750), {ALICE: 250 + 200})
        self.assertEqual(accrue(c, src, 750, 1000), {ALICE: 250 + 300})
        self.assertEqual(fee_buckets(c, 500, 750), [(500, 700)])
        self.assertEqual(fee_buckets(c, 750, 1000), [(700, 900), (900, 1000)])
        self.assertEqual(fee_buckets(c, 705, 895), [])

    def test_quiet_pool_falls_back_to_time(self):
        src = source([HolderEvent(0, ALICE, 10), HolderEvent(150, BOB, 10)])
        credit = accrue(lp_campaign(params=Params(time_bps=2000, fees_bps=8000)), src, 100, 200)
        self.assertEqual(credit, {ALICE: 750, BOB: 250})

    def test_forwarding_through_a_vault(self):
        # The vault holds 30 LP; Carol owns 2/3 of the vault, Dave 1/3.
        src = source(
            [HolderEvent(0, ALICE, 10), HolderEvent(0, VAULT, 30)],
            wrappers={"events": [HolderEvent(0, CAROL, 2), HolderEvent(0, DAVE, 1)]},
        )
        credit = accrue(lp_campaign(amount=4000), src, 100, 200)
        self.assertEqual(credit, {ALICE: 1000, CAROL: 2000, DAVE: 1000})
        no_fwd = accrue(lp_campaign(amount=4000, params=Params(forward=False)), src, 100, 200)
        self.assertEqual(no_fwd, {ALICE: 1000, VAULT: 3000})

    def test_blacklisted_wrapper_is_not_forwarded(self):
        # Review E-7: blacklisting a vault used to pass its LP to the vault's holders anyway.
        src = source(
            [HolderEvent(0, ALICE, 10), HolderEvent(0, VAULT, 30)],
            wrappers={"events": [HolderEvent(0, CAROL, 2), HolderEvent(0, DAVE, 1)]},
        )
        credit = accrue(lp_campaign(amount=4000, params=Params(blacklist=frozenset({VAULT}))), src, 100, 200)
        self.assertEqual(credit, {ALICE: 4000})

    def test_cat_hold(self):
        cat = b"\x5c" * 32
        src = FixtureSource(events={cat: [HolderEvent(0, ALICE, 1), HolderEvent(0, BOB, 3)]})
        c = Campaign(DIST, CREATOR, REWARD, 400, 0, 10, Kind.CAT_HOLD, cat, fee_bps=0)
        self.assertEqual(accrue(c, src, 0, 10), {ALICE: 100, BOB: 300})


class TestRoots(unittest.TestCase):
    def random_world(self, seed):
        rng = random.Random(seed)
        holders = [bytes([i]) * 32 for i in range(1, 12)]
        bal = {h: 0 for h in holders}
        events = []
        for height in sorted(rng.sample(range(0, 1000), 60)):
            h = rng.choice(holders)
            delta = rng.randint(-bal[h], 500)
            if delta:
                bal[h] += delta
                events.append(HolderEvent(height, h, delta))
        fees = [(rng.randint(0, 999), rng.randint(1, 50)) for _ in range(40)]
        campaigns = [
            lp_campaign(amount=rng.randint(10**6, 10**9), start=50, end=900, params=Params(time_bps=6000, fees_bps=4000), fee_bps=300),
            lp_campaign(amount=777_777, start=300, end=650, nonce=1, params=Params(min_balance=100), fee_bps=300),
        ]
        return source(events, fees), campaigns

    def run_all(self, src, campaigns, cuts):
        state = fresh(cuts[0])
        roots = []
        for end in cuts[1:]:
            state, root = run_epoch(state, campaigns, src, end)
            if root is not None:
                roots.append(root)
        return state, roots

    def test_cumulatives_only_grow_and_stay_under_the_schedule(self):
        for seed in range(5):
            src, campaigns = self.random_world(seed)
            _, roots = self.run_all(src, campaigns, [0, 200, 400, 600, 800, 1000])
            prev = {}
            for r in roots:
                now = {(c.campaign_id, c.puzzle_hash): c.cumulative for c in r.claims}
                for key, amount in prev.items():
                    self.assertGreaterEqual(now.get(key, 0), amount)
                for c in campaigns:
                    self.assertLessEqual(r.totals.get(c.id, 0), c.emittable(r.end))
                prev = now

    def test_floor_dust_is_bounded(self):
        src, campaigns = self.random_world(11)
        state, roots = self.run_all(src, campaigns, [0, 1000])
        for c in campaigns:
            holders = len(state.accrued[c.id])
            exact = sum(state.accrued[c.id].values())
            self.assertLess(exact - roots[-1].totals[c.id], holders)

    def test_window_cuts_do_not_change_time_weighted_result(self):
        src = source([HolderEvent(0, ALICE, 3), HolderEvent(120, BOB, 5), HolderEvent(170, ALICE, -2)])
        c = lp_campaign(amount=10**9 + 7)
        a, ra = self.run_all(src, [c], [100, 200])
        b, rb = self.run_all(src, [c], [100, 133, 150, 171, 200])
        self.assertEqual(a.accrued, b.accrued)
        self.assertEqual(ra[-1].root, rb[-1].root)

    def test_proofs_verify_and_claims_are_sorted(self):
        src, campaigns = self.random_world(3)
        _, roots = self.run_all(src, campaigns, [0, 500, 1000])
        for r in roots:
            keys = [(c.campaign_id, c.puzzle_hash) for c in r.claims]
            self.assertEqual(keys, sorted(keys))
            for c in r.claims:
                self.assertEqual(root_from_proof(c.leaf, c.proof), r.root)

    def test_window_with_nothing_accrued_is_not_posted(self):
        src = source([HolderEvent(0, ALICE, 1)])
        c = lp_campaign(start=500, end=600)
        state = fresh()
        same, root = run_epoch(state, [c], src, 200)
        self.assertIsNone(root)
        self.assertIs(same, state)

    def test_reclaimed_campaign_leaves_the_tree(self):
        src = source([HolderEvent(0, ALICE, 1)])
        a, b = lp_campaign(), lp_campaign(nonce=1, end=300)
        state, _ = run_epoch(fresh(), [a, b], src, 200)
        _, root = run_epoch(state, [b], src, 300)
        self.assertEqual(set(root.totals), {b.id})

    def test_unposted_root_is_none_and_posted_root_always_has_a_root(self):
        src = source([HolderEvent(0, ALICE, 1)])
        c = lp_campaign()
        state, root = run_epoch(fresh(), [c], src, 200)
        self.assertIsInstance(root.root, bytes)
        self.assertEqual(root_to_json(root)["root"], root.root.hex())

    def test_state_round_trips(self):
        src, campaigns = self.random_world(5)
        state, roots = self.run_all(src, campaigns, [0, 400])
        self.assertEqual(state_from_json(state_to_json(state)), state)
        self.assertEqual(root_to_json(roots[0])["claim_count"], len(roots[0].claims))


HONEST, ATTACKER = b"\xaa" * 32, b"\xee" * 32


def flash_world(fees):
    """Review probe E (e_engine.py): an honest LP holds 1,000 throughout; the
    attacker adds 99,000 at block 999, the campaign's last."""
    events = [HolderEvent(0, HONEST, 1000), HolderEvent(999, ATTACKER, 99_000)]
    return FixtureSource(lp_assets={POOL: LP}, events={LP: events}, fees={POOL: list(fees)})


def flash_campaign(**params):
    return Campaign(DIST, CREATOR, REWARD, 100_000, 0, 1000, Kind.LP_HOLD, POOL,
                    Params(time_bps=5000, fees_bps=5000, **params), fee_bps=0)


class TestFeeGaming(unittest.TestCase):
    """FLPCR-01: a one-block deposit plus a dust swap must not take the fee half."""

    def test_flash_deposit_and_dust_swap(self):
        credit = accrue(flash_campaign(), flash_world([(999, 1)]), 0, 1000)
        # Before FLPCR-01: {HONEST: 50450.5, ATTACKER: 49549.5}.
        self.assertLessEqual(credit[ATTACKER], Fraction(100_000, 100))  # at most 1% of the budget
        # One block of the time half (99% of block 999's 50) and nothing of the
        # fee half: the swap's lookback ends the block before it, never the live
        # block, so a deposit in the swap's own block is no part of its share.
        self.assertEqual(credit[ATTACKER], Fraction(99 * 50, 100))
        self.assertEqual(sum(credit.values()), 100_000)

    def test_flash_deposit_against_an_honest_swap(self):
        credit = accrue(flash_campaign(), flash_world([(500, 1000), (999, 1000)]), 0, 1000)
        # Before the fix: {HONEST: 75200.5, ATTACKER: 24799.5}.
        self.assertLessEqual(credit[ATTACKER], Fraction(100_000, 100))

    def test_quiet_pool_control_unchanged(self):
        events = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, HONEST, 1000)]})
        self.assertEqual(accrue(flash_campaign(), events, 0, 1000), {HONEST: 100_000})

    def test_lookback_reaches_before_the_campaign(self):
        # A campaign that starts at 5000: the honest LP's history before the
        # start counts in the lookback, so the start is no softer than any block.
        events = [HolderEvent(0, HONEST, 1000), HolderEvent(5000, ATTACKER, 99_000), HolderEvent(5001, ATTACKER, -99_000)]
        src = FixtureSource(lp_assets={POOL: LP}, events={LP: events}, fees={POOL: [(5000, 1)]})
        c = Campaign(DIST, CREATOR, REWARD, 100_000, 5000, 6000, Kind.LP_HOLD, POOL,
                     Params(time_bps=5000, fees_bps=5000), fee_bps=0)
        credit = accrue(c, src, 5000, 6000)
        self.assertEqual(credit[ATTACKER], Fraction(99 * 50, 100))  # one block of time, no fee share


class TestCutIndependence(unittest.TestCase):
    """FLPCR-09: where the operator cuts roots never moves value between holders."""

    def world(self, seed):
        rng = random.Random(seed)
        holders = [bytes([i]) * 32 for i in range(1, 9)]
        bal = {h: 0 for h in holders}
        events = []
        for height in sorted(rng.sample(range(0, 1000), 50)):
            h = rng.choice(holders)
            delta = rng.randint(-bal[h], 800)
            if delta:
                bal[h] += delta
                events.append(HolderEvent(height, h, delta))
        fees = [(rng.randint(0, 999), rng.randint(1, 60)) for _ in range(30)]
        campaigns = [
            lp_campaign(amount=10**9 + seed, start=40, end=910, fee_bps=300,
                        params=Params(time_bps=4000, fees_bps=6000, fee_window=MIN_FEE_WINDOW)),
            lp_campaign(amount=7 * 10**8, start=120, end=700, nonce=1, fee_bps=300,
                        params=Params(time_bps=7000, fees_bps=3000, fee_window=300)),
            lp_campaign(amount=5 * 10**8, start=0, end=1000, nonce=2, fee_bps=300,
                        params=Params(time_bps=5000, fees_bps=5000)),
        ]
        return source(events, fees), campaigns

    @staticmethod
    def posted(src, campaigns, cuts):
        """{height: (accrued, cumulatives)} for every root posted along `cuts`."""
        state, out = fresh(cuts[0]), {}
        for end in cuts[1:]:
            state, root = run_epoch(state, campaigns, src, end)
            if root is not None:
                out[end] = (state.accrued, {(c.campaign_id, c.puzzle_hash): c.cumulative for c in root.claims})
        return state, out

    def test_random_cuts_agree_at_shared_heights(self):
        compared = 0
        for seed in range(6):
            src, campaigns = self.world(seed)
            rng = random.Random(1000 + seed)
            runs = []
            for _ in range(3):
                inner = sorted(rng.sample(range(1, 1000), rng.randint(1, 25)))
                runs.append(self.posted(src, campaigns, [0, *inner, 1000]))
            (state_a, a), (state_b, b), (state_c, c) = runs
            for h in set(a) & set(b):
                self.assertEqual(a[h], b[h], (seed, h))
                compared += 1
            for h in set(a) & set(c):
                self.assertEqual(a[h], c[h], (seed, h))
            self.assertEqual(state_a.accrued, state_b.accrued)
            self.assertEqual(state_a.accrued, state_c.accrued)
        self.assertGreater(compared, 6)

    def test_probe_cut_at_998_changes_nothing(self):
        src = flash_world([(999, 1)])
        c = flash_campaign()
        _, one = self.posted(src, [c], [0, 1000])
        _, two = self.posted(src, [c], [0, 998, 1000])
        # Before the fix: one root {HONEST: 50450, ATTACKER: 49549}, cut at 998 {HONEST: 99851, ATTACKER: 148}.
        self.assertEqual(one[1000], two[1000])


class TestLookbackExcludesTheLiveBlock(unittest.TestCase):
    """The owner's rule (2026-10-19): shares come from past holdings,
    time-averaged, never the live block; a position held for one or two blocks
    averages to about zero."""

    W = MIN_FEE_WINDOW

    def campaign(self):
        return Campaign(DIST, CREATOR, REWARD, 10**9, 1000, 2000, Kind.LP_HOLD, POOL,
                        Params(time_bps=5000, fees_bps=5000, fee_window=self.W), fee_bps=0)

    def share(self, events, h):
        c = self.campaign()
        src = FixtureSource(lp_assets={POOL: LP}, events={LP: events})
        return Weights(c, Holdings(src, LP, h + 1, True), Requirements(c, src, h + 1)).lookback_share(h)

    def test_a_deposit_in_the_swaps_block_is_worth_nothing_to_it(self):
        share = self.share([HolderEvent(0, HONEST, 1000), HolderEvent(1500, ATTACKER, 10**9)], 1500)
        self.assertEqual(share, {HONEST: 1})
        # and nothing of the fee half in accrual, however large the deposit
        src = FixtureSource(lp_assets={POOL: LP}, fees={POOL: [(1500, 10**6)]},
                            events={LP: [HolderEvent(0, HONEST, 1000), HolderEvent(1500, ATTACKER, 10**9),
                                         HolderEvent(1501, ATTACKER, -10**9)]})
        credit = accrue(self.campaign(), src, 1000, 2000)
        time_only = Fraction(10**9 // 2 // 1000) * Fraction(10**9, 10**9 + 1000)  # its one block of the time half
        self.assertEqual(credit[ATTACKER], time_only)

    def test_the_block_before_counts(self):
        share = self.share([HolderEvent(0, HONEST, 1), HolderEvent(1499, ATTACKER, 1)], 1500)
        self.assertEqual(share[ATTACKER], Fraction(1, 2 * self.W))

    def test_one_or_two_blocks_average_to_about_zero(self):
        for held in (1, 2):
            # a position a million times the honest LP's, opened `held` blocks before the swap
            share = self.share([HolderEvent(0, HONEST, 1), HolderEvent(1500 - held, ATTACKER, 10**6),
                                HolderEvent(1500, ATTACKER, -10**6)], 1500)
            self.assertLess(share[ATTACKER], Fraction(held, self.W))
            self.assertLess(share[ATTACKER], Fraction(11, 1000))  # under 1.1% even at the floor
        # and at the default window, under 1/2000
        self.W = BLOCKS_PER_DAY
        share = self.share([HolderEvent(0, HONEST, 1), HolderEvent(9998, ATTACKER, 10**6)], 10_000)
        self.assertLess(share[ATTACKER], Fraction(1, 2000))

    def test_an_event_at_height_zero_has_no_past(self):
        self.assertEqual(self.share([HolderEvent(0, HONEST, 1)], 0), {})


class TestDailyEmission(unittest.TestCase):
    """The owner's rule: the front end sets the total and the length in days;
    emission per day is total / days, equal every day."""

    def test_every_day_releases_the_same(self):
        for amount, days in ((1_000_000_007, 30), (10**12, 7), (999, 365), (5 * 10**12 + 3, 1)):
            c = Campaign(DIST, CREATOR, REWARD, amount, 10_000, 10_000 + days * BLOCKS_PER_DAY, Kind.LP_HOLD, POOL)
            per_day = daily_emission(c)
            self.assertEqual(per_day, Fraction(c.budget, days))
            released = [c.emitted_between(c.start + d * BLOCKS_PER_DAY, c.start + (d + 1) * BLOCKS_PER_DAY)
                        for d in range(days)]
            self.assertEqual(sum(released), c.budget)
            for r in released:
                self.assertLess(abs(r - per_day), 1)  # whole mojos: the floor or the ceiling of total / days

    def test_the_budget_is_the_total_less_the_creation_fee(self):
        c = Campaign(DIST, CREATOR, REWARD, 1_000_000, 0, 10 * BLOCKS_PER_DAY, Kind.LP_HOLD, POOL)
        self.assertEqual(daily_emission(c), Fraction(970_000, 10))

    def test_equal_days_are_paid_whoever_holds(self):
        # Two holders trading places mid-day: each day still pays out exactly its release.
        events = [HolderEvent(0, ALICE, 5), HolderEvent(BLOCKS_PER_DAY + 77, BOB, 20),
                  HolderEvent(2 * BLOCKS_PER_DAY + 5, ALICE, -5)]
        c = Campaign(DIST, CREATOR, REWARD, 3 * 10**9, 0, 3 * BLOCKS_PER_DAY, Kind.LP_HOLD, POOL, fee_bps=0)
        src = source(events)
        for d in range(3):
            day = accrue(c, src, d * BLOCKS_PER_DAY, (d + 1) * BLOCKS_PER_DAY)
            self.assertEqual(sum(day.values()), 10**9)


class TestExactWeights(unittest.TestCase):
    """FLPCR-17: fee weights are exact rationals; a float is refused at the Source boundary."""

    def campaign(self):
        return lp_campaign(amount=10**12, start=0, end=1000, params=Params(time_bps=5000, fees_bps=5000, fee_window=200))

    def test_float_weight_refused(self):
        src = source([HolderEvent(0, ALICE, 3)], fees=[(100, 0.1)])
        with self.assertRaises(TypeError):
            src.fee_events(POOL, 0, 1000)
        with self.assertRaises(TypeError):
            accrue(self.campaign(), src, 0, 1000)

    def test_float_weight_refused_from_json(self):
        with self.assertRaises(TypeError):
            FixtureSource.from_json({"fees": {POOL.hex(): [[100, 0.1]]}})
        with self.assertRaises(TypeError):
            FixtureSource.from_json({"fees": {POOL.hex(): [[100, [1, 0.5]]]}})

    def test_float_from_a_custom_source_refused(self):
        class FloatSource(FixtureSource):
            def fee_events(self, pool_launcher, start, end):
                return [(100, 0.1)]

        src = FloatSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, ALICE, 3)]})
        with self.assertRaises(TypeError):
            accrue(self.campaign(), src, 0, 1000)

    def test_rational_weights_stay_exact(self):
        src = FixtureSource.from_json({
            "lp_assets": {POOL.hex(): LP.hex()},
            "events": {LP.hex(): [[0, ALICE.hex(), 3], [500, BOB.hex(), 7]]},
            "fees": {POOL.hex(): [[100, [1, 10]], [600, [1, 5]], [700, 2]]},
        })
        self.assertEqual(src.fee_events(POOL, 0, 1000), [(100, Fraction(1, 10)), (600, Fraction(1, 5)), (700, 2)])
        credit = accrue(self.campaign(), src, 0, 1000)
        self.assertTrue(all(isinstance(v, Fraction) for v in credit.values()))
        state, _ = run_epoch(fresh(0), [self.campaign()], src, 1000)
        self.assertEqual(state_from_json(state_to_json(state)), state)


if __name__ == "__main__":
    unittest.main()
