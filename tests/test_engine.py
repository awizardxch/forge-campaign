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
from flpcr.manifest import root_to_json, state_from_json, state_to_json

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

    def test_fee_weighting_rewards_liquidity_present_when_used(self):
        # Alice holds throughout; Bob is in only for [150, 200). Every fee is paid at 160.
        src = source(
            [HolderEvent(0, ALICE, 10), HolderEvent(150, BOB, 10)],
            fees=[(160, 7)],
        )
        params = Params(time_bps=5000, fees_bps=5000)
        credit = accrue(lp_campaign(params=params), src, 100, 200)
        # time half: Alice 250 + 125, Bob 125; fee half: 250 each
        self.assertEqual(credit[ALICE], 625)
        self.assertEqual(credit[BOB], 375)

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
        state = EngineState(DIST, height=cuts[0])
        roots = []
        for end in cuts[1:]:
            state, root = run_epoch(state, campaigns, src, end)
            if root.root is not None:
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
        state = EngineState(DIST, height=100)
        same, root = run_epoch(state, [c], src, 200)
        self.assertIsNone(root.root)
        self.assertIs(same, state)

    def test_reclaimed_campaign_leaves_the_tree(self):
        src = source([HolderEvent(0, ALICE, 1)])
        a, b = lp_campaign(), lp_campaign(nonce=1, end=300)
        state, _ = run_epoch(EngineState(DIST, height=100), [a, b], src, 200)
        _, root = run_epoch(state, [b], src, 300)
        self.assertEqual(set(root.totals), {b.id})

    def test_state_round_trips(self):
        src, campaigns = self.random_world(5)
        state, roots = self.run_all(src, campaigns, [0, 400])
        self.assertEqual(state_from_json(state_to_json(state)), state)
        self.assertEqual(root_to_json(roots[0])["claim_count"], len(roots[0].claims))


if __name__ == "__main__":
    unittest.main()
