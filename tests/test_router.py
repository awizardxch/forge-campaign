import unittest

from flpcr import Campaign, Kind
from flpcr.router import XCH, ForgeRegistry, NotRoutable, RouterRules, check_campaign

DIST = b"\xd1" * 32
CREATOR = b"\xcc" * 32
POOL = b"\xb0" * 32
WUSDC, SPACEBUCKS, UNLISTED = b"\x01" * 32, b"\x02" * 32, b"\x03" * 32

REGISTRY = ForgeRegistry.from_pools({POOL: [XCH, WUSDC, SPACEBUCKS]})


def campaign(reward, kind, target):
    return Campaign(DIST, CREATOR, reward, 1000, 100, 200, kind, target)


class TestRouter(unittest.TestCase):
    def test_registry_assets_and_xch_are_routable(self):
        check_campaign(campaign(WUSDC, Kind.LP_HOLD, POOL), REGISTRY)
        check_campaign(campaign(XCH, Kind.CAT_HOLD, SPACEBUCKS), REGISTRY)
        check_campaign(campaign(XCH, Kind.LP_HOLD, POOL), ForgeRegistry.from_pools({POOL: [WUSDC]}))

    def test_unlisted_reward_refused(self):
        with self.assertRaises(NotRoutable):
            check_campaign(campaign(UNLISTED, Kind.LP_HOLD, POOL), REGISTRY)

    def test_unregistered_pool_refused(self):
        with self.assertRaises(NotRoutable):
            check_campaign(campaign(WUSDC, Kind.LP_HOLD, b"\xb1" * 32), REGISTRY)

    def test_unlisted_cat_target_refused(self):
        with self.assertRaises(NotRoutable):
            check_campaign(campaign(WUSDC, Kind.CAT_HOLD, UNLISTED), REGISTRY)

    def test_rules_floor_and_creator_cap(self):
        rules = RouterRules(min_amount={XCH: 1000}, min_per_day={XCH: 4608 * 10}, max_live_per_creator=2)
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
