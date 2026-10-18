import unittest

from flpcr import Campaign, Kind
from flpcr.router import XCH, ForgeRegistry, NotRoutable, check_campaign

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


if __name__ == "__main__":
    unittest.main()
