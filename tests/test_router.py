import unittest

from flpcr import Campaign, Kind, Params
from flpcr.model import BLOCKS_PER_DAY, MIN_FEE_WINDOW
from flpcr.router import XCH, CatPolicy, ForgeRegistry, NotRoutable, RouterRules, check_campaign, usd_floor_mojos

DIST = b"\xd1" * 32
CREATOR = b"\xcc" * 32
POOL = b"\xb0" * 32
WUSDC, SPACEBUCKS, UNLISTED = b"\x01" * 32, b"\x02" * 32, b"\x03" * 32

REGISTRY = ForgeRegistry.from_pools({POOL: [XCH, WUSDC, SPACEBUCKS]})


OPEN = RouterRules.permissive()  # these tests are about the registry and the named rules


def campaign(reward, kind, target):
    return Campaign(DIST, CREATOR, reward, 1000, 100, 200, kind, target)


class TestRouter(unittest.TestCase):
    def test_registry_assets_and_xch_are_routable(self):
        check_campaign(campaign(WUSDC, Kind.LP_HOLD, POOL), REGISTRY, OPEN)
        check_campaign(campaign(XCH, Kind.CAT_HOLD, SPACEBUCKS), REGISTRY, OPEN)
        check_campaign(campaign(XCH, Kind.LP_HOLD, POOL), ForgeRegistry.from_pools({POOL: [WUSDC]}), OPEN)

    def test_unlisted_reward_refused(self):
        with self.assertRaises(NotRoutable):
            check_campaign(campaign(UNLISTED, Kind.LP_HOLD, POOL), REGISTRY, OPEN)

    def test_unregistered_pool_refused(self):
        with self.assertRaises(NotRoutable):
            check_campaign(campaign(WUSDC, Kind.LP_HOLD, b"\xb1" * 32), REGISTRY, OPEN)

    def test_unlisted_cat_target_refused(self):
        with self.assertRaises(NotRoutable):
            check_campaign(campaign(WUSDC, Kind.CAT_HOLD, UNLISTED), REGISTRY, OPEN)

    def test_rules_floor_and_creator_cap(self):
        rules = RouterRules.permissive(min_amount={XCH: 1000}, min_per_day={XCH: 4608 * 10}, max_live_per_creator=2)
        check_campaign(campaign(XCH, Kind.LP_HOLD, POOL), REGISTRY, rules)  # 1000 over 100 blocks
        small = Campaign(DIST, CREATOR, XCH, 999, 100, 200, Kind.LP_HOLD, POOL)
        with self.assertRaises(NotRoutable):
            check_campaign(small, REGISTRY, rules)
        slow = Campaign(DIST, CREATOR, XCH, 1000, 100, 1100, Kind.LP_HOLD, POOL)  # 1 per block < 10
        with self.assertRaises(NotRoutable):
            check_campaign(slow, REGISTRY, rules)
        with self.assertRaises(NotRoutable):
            check_campaign(campaign(XCH, Kind.LP_HOLD, POOL), REGISTRY, rules, live_for_creator=2)
        check_campaign(campaign(WUSDC, Kind.LP_HOLD, POOL), REGISTRY, rules)  # no floor set for WUSDC

    def test_claim_window_between_48_hours_and_30_days(self):
        """Owner, 2026-11-27: holders get about 48 hours after a campaign ends, then the creator may
        reclaim; no campaign holds its slot longer than 30 days past its end."""
        import dataclasses
        rules = RouterRules.permissive(min_claim_window=RouterRules().min_claim_window,
                                       max_claim_window=RouterRules().max_claim_window)
        self.assertEqual((RouterRules().min_claim_window, RouterRules().max_claim_window),
                         (2 * BLOCKS_PER_DAY, 30 * BLOCKS_PER_DAY))
        base = campaign(XCH, Kind.LP_HOLD, POOL)
        for ok in (2 * BLOCKS_PER_DAY, 30 * BLOCKS_PER_DAY):
            check_campaign(dataclasses.replace(base, claim_window=ok), REGISTRY, rules)
        for bad, why in ((2 * BLOCKS_PER_DAY - 1, "under"), (30 * BLOCKS_PER_DAY + 1, "over")):
            with self.assertRaisesRegex(NotRoutable, f"claim window is {why}"):
                check_campaign(dataclasses.replace(base, claim_window=bad), REGISTRY, rules)


class TestFeeWindowFloor(unittest.TestCase):
    """The owner's rule: a short-held position averages to about zero. The
    fee window sets how close to zero; Params refuses under an hour on every
    network, and the production router signs for a day or more."""

    def lp(self, window):
        return Campaign(DIST, CREATOR, XCH, 1000, 100, 200, Kind.LP_HOLD, POOL,
                        Params(time_bps=5000, fees_bps=5000, fee_window=window))

    def test_production_router_wants_a_day(self):
        check_campaign(self.lp(BLOCKS_PER_DAY), REGISTRY, OPEN)
        with self.assertRaises(NotRoutable):
            check_campaign(self.lp(BLOCKS_PER_DAY - 1), REGISTRY, OPEN)

    def test_a_testnet_router_may_go_down_to_the_protocol_floor(self):
        testnet = RouterRules.permissive(min_fee_window=MIN_FEE_WINDOW)
        check_campaign(self.lp(MIN_FEE_WINDOW), REGISTRY, testnet)
        with self.assertRaises(ValueError):
            RouterRules(min_fee_window=MIN_FEE_WINDOW - 1)
        with self.assertRaises(ValueError):
            self.lp(MIN_FEE_WINDOW - 1)


class TestCatFloor(unittest.TestCase):
    """Owner, 2026-11-11: a CAT reward's floor is the equivalent of $1 at its Forge price, set by
    the router; the operator can turn a CAT off for campaigns or set its minimum."""

    RULES = RouterRules(kinds=frozenset({Kind.LP_HOLD}), min_claim_window=0)  # the service's default CAT path

    def cat(self, amount):
        return Campaign(DIST, CREATOR, WUSDC, amount, 100, 200, Kind.LP_HOLD, POOL)

    def test_usd_floor_rounds_up_in_cat_mojos(self):
        self.assertEqual(usd_floor_mojos(1.0), 1000)  # $1 a token: one token
        self.assertEqual(usd_floor_mojos(0.25), 4000)
        self.assertEqual(usd_floor_mojos(3.0), 334)  # 333.33… rounds up
        self.assertEqual(usd_floor_mojos("0.000123"), 8130082)
        for bad in (None, 0, -1, "nan", "inf", "x", True):
            self.assertIsNone(usd_floor_mojos(bad), bad)

    def test_a_cat_reward_needs_the_routers_word(self):
        with self.assertRaisesRegex(NotRoutable, "could not be read"):
            check_campaign(self.cat(10**9), REGISTRY, self.RULES)
        check_campaign(self.cat(1000), REGISTRY, self.RULES, cat_policy=CatPolicy(price_usd=1.0))

    def test_amount_under_the_dollar_floor_is_refused(self):
        policy = CatPolicy(price_usd=0.5)  # $1 = 2 tokens = 2000 mojos
        check_campaign(self.cat(2000), REGISTRY, self.RULES, cat_policy=policy)
        with self.assertRaisesRegex(NotRoutable, "floor of 2,000 mojos .the equivalent of \\$1"):
            check_campaign(self.cat(1999), REGISTRY, self.RULES, cat_policy=policy)

    def test_no_price_no_floor_no_campaign(self):
        with self.assertRaisesRegex(NotRoutable, "no Forge price"):
            check_campaign(self.cat(10**12), REGISTRY, self.RULES, cat_policy=CatPolicy(price_usd=None))

    def test_operator_minimum_wins_over_the_price(self):
        policy = CatPolicy(price_usd=0.5, min_amount=10_000)
        with self.assertRaisesRegex(NotRoutable, "operator's minimum"):
            check_campaign(self.cat(9_999), REGISTRY, self.RULES, cat_policy=policy)
        check_campaign(self.cat(10_000), REGISTRY, self.RULES, cat_policy=policy)
        # A minimum with no price still sets a floor.
        check_campaign(self.cat(10_000), REGISTRY, self.RULES, cat_policy=CatPolicy(min_amount=10_000))

    def test_operator_switch_turns_a_cat_off(self):
        with self.assertRaisesRegex(NotRoutable, "turned this CAT off"):
            check_campaign(self.cat(10**12), REGISTRY, self.RULES, cat_policy=CatPolicy(enabled=False, price_usd=1.0))

    def test_xch_ignores_cat_policy(self):
        check_campaign(Campaign(DIST, CREATOR, XCH, 10**11, 100, 200, Kind.LP_HOLD, POOL), REGISTRY, self.RULES)

    def test_static_rules_floor_still_applies_to_a_cat(self):
        rules = RouterRules(kinds=frozenset({Kind.LP_HOLD}), min_claim_window=0, min_amount={WUSDC: 5000})
        with self.assertRaisesRegex(NotRoutable, "below the minimum for this reward asset"):
            check_campaign(self.cat(4000), REGISTRY, rules, cat_policy=CatPolicy(price_usd=1.0))


class TestNetworks(unittest.TestCase):
    TESTNET = "txch1zm9z9h4fjn88qa7f82a9n5lga2qyydd8l53jqw9qm2z5mh0ge6kqwt5jps"
    MAINNET = "xch1gfpyysjzgfpyysjzgfpyysjzgfpyysjzgfpyysjzgfpyysjzgfpqcjkap4"

    def setUp(self):
        try:
            import chia_wallet_sdk  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("chia-wallet-sdk missing")

    def test_testnet_default_is_the_dev_multisig(self):
        from flpcr.networks import addresses

        a = addresses("testnet11", env={})
        self.assertEqual(a.dev_fee_ph.hex(), "16ca22dea994ce7077c93aba59d3e8ea804235a7fd232038a0da854ddde8ceac")
        self.assertEqual(a.admin_ph, a.dev_fee_ph)

    def test_mainnet_reads_only_the_env_var(self):
        from flpcr.networks import MAINNET_DEV_FEE_ENV, AddressError, addresses

        with self.assertRaises(AddressError):
            addresses("mainnet", env={})
        a = addresses("mainnet", env={MAINNET_DEV_FEE_ENV: self.MAINNET})
        self.assertEqual(a.dev_fee_ph.hex(), "4242424242424242424242424242424242424242424242424242424242424242")
        with self.assertRaises(AddressError):
            addresses("mainnet", env={MAINNET_DEV_FEE_ENV: self.TESTNET})  # wrong network


if __name__ == "__main__":
    unittest.main()
