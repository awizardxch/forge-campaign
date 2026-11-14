"""The claim bundle's fee rule (flpcr/fees.py): 0 while the mempool has room, the 5 mojos/cost
floor (+10%) only when it is full, never a nonzero fee under the floor."""

import unittest

from flpcr.fees import FLOOR_PER_COST, bundle_fee, bundle_fee_from_node

MAX = 110_000_000_000  # a full node's mempool_max_total_cost (10 blocks)
COST = 446_000_000     # the 3-generation claim-all bundle of 2026-11-13 (~416.9M + signatures + fee coin)


class TestBundleFee(unittest.TestCase):
    def test_room_pays_nothing(self):
        self.assertEqual(bundle_fee(COST, 31_800_000_000, MAX), 0)
        self.assertEqual(bundle_fee(COST, 0, MAX), 0)

    def test_full_pays_the_floor_plus_a_margin(self):
        fee = bundle_fee(COST, 99_000_000_000, MAX)
        self.assertGreaterEqual(fee, FLOOR_PER_COST * COST)  # never under the cliff
        self.assertEqual(fee, COST * 55 // 10)

    def test_the_line_counts_this_bundle(self):
        edge = int(0.7 * MAX) - COST
        self.assertEqual(bundle_fee(COST, edge, MAX), 0)
        self.assertGreater(bundle_fee(COST, edge + 1, MAX), 0)

    def test_no_reading_counts_as_full(self):
        self.assertGreater(bundle_fee(COST, None, None), 0)
        self.assertGreater(bundle_fee_from_node(COST, {}), 0)

    def test_from_a_node_answer(self):
        state = {"mempool_cost": 43_800_000_000, "mempool_max_total_cost": MAX}
        self.assertEqual(bundle_fee_from_node(COST, state), 0)

    def test_the_old_rule_for_comparison(self):
        # claim-all used 10 mojos/cost always: 4,169,231,040 mojos for this bundle on 2026-11-13
        self.assertEqual(10 * 416_923_104, 4_169_231_040)


if __name__ == "__main__":
    unittest.main()
