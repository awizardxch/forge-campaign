"""Forge V20 (protocol 21) pools as campaign targets, the only revision new campaigns target (owner,
2026-11-25). Fee events are read from real mainnet spends (`fixtures/v20_pool_spends.json`, the
node's own answers for three V20 XCH pools: their adds, removes and swaps), each swap's gross input
was checked against the pool's own reserve change when recorded, and the LP asset proves the
revision. V19 (protocol 20) is not pinned.
"""

import json
import pathlib
import unittest
from fractions import Fraction

from chia_wallet_sdk import Coin

import flpcr.v16_fees as fees
from flpcr.v16_fees import PROTOCOL_V18, PROTOCOL_V20, PoolFeeWalk, UnsupportedPool, check_protocol, lp_asset_id

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "fixtures" / "v20_pool_spends.json").read_text(encoding="utf-8"))
POOLS = {p["name"]: p for p in FIXTURE["pools"]}


def h(x: str) -> bytes:
    return bytes.fromhex(x.removeprefix("0x"))


class RecordedNode:
    """A full node that knows only the recorded coins and spends."""

    def __init__(self, data=FIXTURE):
        self.records = {}
        for r in data["coin_records"]:
            c = r["coin"]
            self.records[Coin(h(c["parent_coin_info"]), h(c["puzzle_hash"]), int(c["amount"])).coin_id()] = r
        self.spends = data["spends"]

    def __call__(self, endpoint, body):
        if endpoint == "get_coin_record_by_name":
            return {"coin_record": self.records.get(h(body["name"]))}
        if endpoint == "get_coin_records_by_parent_ids":
            parents = {h(p) for p in body["parent_ids"]}
            return {"coin_records": [r for r in self.records.values() if h(r["coin"]["parent_coin_info"]) in parents]}
        if endpoint == "get_puzzle_and_solution":
            return {"coin_solution": self.spends[body["coin_id"].removeprefix("0x")]}
        raise AssertionError(f"unexpected call {endpoint}")


def walk(pool: bytes, protocol=PROTOCOL_V20) -> PoolFeeWalk:
    w = PoolFeeWalk(pool, protocol)
    w.update(RecordedNode())
    return w


class TestV20Pins(unittest.TestCase):
    def test_recorded_at_the_pinned_commit(self):
        self.assertEqual(FIXTURE["forge_v20_commit"], fees.FORGE_V20_COMMIT)
        self.assertEqual(len(fees.V20_LEAF_MOD_HASHES), 8)
        self.assertEqual(fees.MAX_PROTOCOL, PROTOCOL_V20)

    def test_the_shared_leaves_are_v18s(self):
        """V20 kept V18's swap, observe, collect and dao_fee builds; the rest are its own."""
        for name in ("forge_action_swap", "forge_action_observe", "forge_action_collect", "forge_action_dao_fee"):
            self.assertEqual(fees.V20_LEAF_MOD_HASHES[name], fees.V18_LEAF_MOD_HASHES[name], name)
        for name in ("forge_action_add", "forge_action_remove", "forge_action_rebase", "forge_action_refund"):
            self.assertNotEqual(fees.V20_LEAF_MOD_HASHES[name], fees.V18_LEAF_MOD_HASHES[name], name)
        self.assertNotEqual(fees.V20_LP_TAIL_MOD_HASH, fees.V18_LP_TAIL_MOD_HASH)

    def test_real_mainnet_v20_pools_derive_under_21_only(self):
        for name, pool in POOLS.items():
            launcher, lp = h(pool["launcher_id"]), h(pool["lp_asset_id"])
            self.assertEqual(check_protocol(launcher, lp), PROTOCOL_V20, name)
            self.assertEqual(check_protocol(launcher, lp, PROTOCOL_V20), PROTOCOL_V20, name)
            for other in (17, 18, 19):
                self.assertNotEqual(lp_asset_id(launcher, other), lp, name)
                with self.assertRaises(UnsupportedPool):
                    check_protocol(launcher, lp, other)

    def test_v19_is_not_pinned(self):
        pool = next(iter(POOLS.values()))
        with self.assertRaisesRegex(UnsupportedPool, "protocol 20: no fee reader is pinned"):
            check_protocol(h(pool["launcher_id"]), h(pool["lp_asset_id"]), 20)
        with self.assertRaisesRegex(UnsupportedPool, "protocol 22: no fee reader is pinned"):
            check_protocol(h(pool["launcher_id"]), h(pool["lp_asset_id"]), 22)


class TestV20Fees(unittest.TestCase):
    def test_every_recorded_spend_and_swap_is_read(self):
        for name, pool in POOLS.items():
            got = walk(h(pool["launcher_id"]))
            self.assertEqual(got.spends, pool["spends"], name)
            want = [x for x in FIXTURE["expected_swaps"] if x["pool"] == name]
            self.assertTrue(want, name)
            self.assertEqual([(s.height, s.asset_in, s.asset_out, s.gross_input, s.lp_fee) for s in got.swaps],
                             [(x["height"], x["asset_in"], x["asset_out"], x["gross_input"], x["lp_fee"]) for x in want])
            self.assertEqual([s.weight for s in got.swaps], [Fraction(*x["weight"]) for x in want])

    def test_one_percent_pools_pay_one_percent(self):
        for s in FIXTURE["expected_swaps"]:
            self.assertEqual(s["lp_fee"], fees.lp_fee(s["gross_input"], 100))

    def test_a_v20_pool_is_not_read_as_v18(self):
        """The add/remove leaves are V20's own builds, so a V20 pool's walk fails under 19."""
        pool = h(POOLS["💎 XCH"]["launcher_id"])
        with self.assertRaisesRegex(UnsupportedPool, "not protocol 19"):
            walk(pool, PROTOCOL_V18)

    def test_the_walk_round_trips(self):
        pool = h(POOLS["💎 XCH"]["launcher_id"])
        w = walk(pool)
        back = PoolFeeWalk.from_json(json.loads(json.dumps(w.to_json())))
        self.assertEqual(back.swaps, w.swaps)
        self.assertEqual(back.protocol, PROTOCOL_V20)


if __name__ == "__main__":
    unittest.main()
