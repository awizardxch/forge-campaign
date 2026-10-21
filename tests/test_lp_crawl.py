"""The LP crawl's bookkeeping: coins become balance events for their inner
puzzle hash, custody is not a position, and a crawl survives its cache.
(The chain walk itself is exercised against testnet11: every V16 pool's
crawled holdings plus its burned floor equal the pool's total_lp.)"""

import unittest

from flpcr import EngineState, Kind, run_epoch
from flpcr.lp_crawl import BURN_PUZZLE_HASH, LP_MINT_INNER_HASH, OFFER_MOD_HASH, LpCoin, LpCrawl
from flpcr.model import Campaign

ASSET = b"\xe2" * 32
POOL = b"\xb0" * 32
ALICE, BOB = b"\xa1" * 32, b"\xb2" * 32


def coin(n, inner, amount, created, spent=0, parent=b"\x00" * 32):
    return LpCoin(bytes([n]) * 32, parent, b"\xcc" * 32, amount, inner, created, spent)


def crawl(*coins):
    c = LpCrawl(ASSET)
    for x in coins:
        c.coins[x.coin_id] = x
    return c


class TestEvents(unittest.TestCase):
    def setUp(self):
        self.c = crawl(
            coin(1, LP_MINT_INNER_HASH, 1, 100, 101),  # the mint eve: custody
            coin(2, ALICE, 900, 101, 150),               # Alice's mint, later split
            coin(3, BURN_PUZZLE_HASH, 1, 101),           # the locked floor
            coin(4, ALICE, 600, 150),                    # Alice keeps 600
            coin(5, OFFER_MOD_HASH, 300, 150, 150),      # through a settlement...
            coin(6, BOB, 300, 150),                      # ...to Bob
        )

    def test_balances_follow_the_coins(self):
        self.assertEqual(self.c.balances(), {ALICE: 600, BOB: 300})
        self.assertEqual(self.c.balances(at=120), {ALICE: 900})

    def test_custody_is_not_a_position(self):
        holders = {e.puzzle_hash for e in self.c.events()}
        self.assertEqual(holders, {ALICE, BOB})

    def test_events_before_a_height(self):
        self.assertEqual([(e.height, e.delta) for e in self.c.events(before=150)], [(101, 900)])

    def test_the_cache_round_trips(self):
        self.c.expanded = {bytes([2]) * 32}
        again = LpCrawl.from_json(self.c.to_json())
        self.assertEqual(again.balances(), self.c.balances())
        self.assertEqual(again.expanded, self.c.expanded)

    def test_every_holder_is_in_the_root(self):
        """The engine pays by time-weighted share of the LP held: Alice 900
        alone, then Alice 600 and Bob 300; a holder who never claims is still
        in the root (their share returns to the creator at reclaim)."""
        class Src:
            def lp_asset(_, pool):
                return ASSET

            def holder_events(_, asset, before):
                return self.c.events(before)

            def fee_events(_, *a):
                return []

            def nft_events(_, *a):
                return []

            def links(_):
                return []

            def excluded(_, asset):
                return set()

            def forwarders(_, asset):
                return {}

        camp = Campaign(b"\xd1" * 32, b"\xc0" * 32, bytes(32), 1_030_928, 120, 180, Kind.LP_HOLD, POOL)
        _, root = run_epoch(EngineState(b"\xd1" * 32, height=camp.start), [camp], Src(), camp.end)
        got = {cl.puzzle_hash: cl.cumulative for cl in root.claims}
        self.assertEqual(set(got), {ALICE, BOB})
        # 30 blocks Alice alone, 30 blocks 2:1 -> Alice 30 + 20 = 50 of 60 parts, Bob 10.
        self.assertAlmostEqual(got[ALICE] / (got[ALICE] + got[BOB]), 50 / 60, places=3)


if __name__ == "__main__":
    unittest.main()
