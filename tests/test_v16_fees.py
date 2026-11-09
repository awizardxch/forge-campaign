"""Fee events from Forge V16 pool spends (flpcr.v16_fees, LiveLpSource.fee_events).

The fixture `fixtures/v16_pool_spends.json` holds real V16 pool spends: built
offline by forge-puzzles' own driver (`forge_v16_driver.spend_actions`) and
accepted by its mempool validator, recorded as a node would answer them
(`scripts/record_v16_fee_fixture.py`). Coins and heights are fabricated; the
puzzles and solutions are V16's. It also records the swaps the driver was asked
to build, which the reader must find again from the chain data alone.
"""

import json
import os
import pathlib
import unittest
from fractions import Fraction
from types import SimpleNamespace

from chia_wallet_sdk import Clvm, Coin

from flpcr import Campaign, EngineState, FixtureSource, HolderEvent, Kind, Params, accrue, run_epoch
from flpcr.live_source import LiveLpSource, parse_forge_pools
from flpcr.sources import fee_weight
from flpcr.v16_fees import (
    ACTION_LAYER_HASH, FORGE_PUZZLES_COMMIT, LEAF_MOD_HASHES, LP_TAIL_MOD_HASH, FeeReadError, PoolFeeWalk,
    UnsupportedPool, check_protocol, lp_fee, parse_pool_spend, v16_lp_asset_id,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "fixtures" / "v16_pool_spends.json").read_text())
POOLS = {p["name"]: p for p in FIXTURE["pools"]}
XCH_POOL = bytes.fromhex(POOLS["xch-cat"]["launcher_id"])
CAT_POOL = bytes.fromhex(POOLS["cat-cat-3-1"]["launcher_id"])


def h(x: str) -> bytes:
    return bytes.fromhex(x.removeprefix("0x"))


class RecordedNode:
    """A full node that knows only the recorded coins and spends."""

    def __init__(self, data=FIXTURE):
        self.records = {}
        for r in data["coin_records"]:
            c = r["coin"]
            self.records[Coin(h(c["parent_coin_info"]), h(c["puzzle_hash"]), c["amount"]).coin_id()] = r
        self.spends = data["spends"]
        self.calls = []

    def __call__(self, endpoint, body):
        self.calls.append(endpoint)
        if endpoint == "get_coin_record_by_name":
            return {"coin_record": self.records.get(h(body["name"]))}
        if endpoint == "get_coin_records_by_parent_ids":
            parents = {h(p) for p in body["parent_ids"]}
            return {"coin_records": [r for r in self.records.values() if h(r["coin"]["parent_coin_info"]) in parents]}
        if endpoint == "get_puzzle_and_solution":
            return {"coin_solution": self.spends[body["coin_id"].removeprefix("0x")]}
        raise AssertionError(f"unexpected call {endpoint}")


def walk(pool):
    w = PoolFeeWalk(pool)
    w.update(RecordedNode())
    return w


class TestFixture(unittest.TestCase):
    def test_recorded_at_the_pinned_commit(self):
        self.assertEqual(FIXTURE["forge_puzzles_commit"], FORGE_PUZZLES_COMMIT)

    def test_pins_match_the_forge_puzzles_checkout(self):
        fp = pathlib.Path(os.environ.get("FORGE_PUZZLES") or ROOT.parent / "awizardxch" / "forge-puzzles")
        compiled = fp / "contracts" / "v16" / "compiled"
        if not compiled.is_dir():
            self.skipTest("no forge-puzzles checkout (set FORGE_PUZZLES)")
        read = lambda name: bytes.fromhex((compiled / f"{name}.rue.hash").read_text().strip())  # noqa: E731
        self.assertEqual(read("action"), ACTION_LAYER_HASH)
        self.assertEqual(read("forge_lp_cat_tail"), LP_TAIL_MOD_HASH)
        for name, pinned in LEAF_MOD_HASHES.items():
            self.assertEqual(read(name), pinned, name)


class TestReader(unittest.TestCase):
    def test_every_swap_is_found_with_its_fee(self):
        for name, pool in POOLS.items():
            got = walk(h(pool["launcher_id"])).swaps
            want = [s for s in FIXTURE["expected_swaps"] if s["pool"] == name]
            self.assertEqual([(s.height, s.asset_in, s.asset_out, s.gross_input, s.lp_fee) for s in got],
                             [(s["height"], s["asset_in"], s["asset_out"], s["gross_input"], s["lp_fee"]) for s in want])

    def test_weight_is_the_fee_at_the_pre_spend_spot(self):
        for name, pool in POOLS.items():
            w = pool["weights"]
            for s, x in zip(walk(h(pool["launcher_id"])).swaps, [s for s in FIXTURE["expected_swaps"] if s["pool"] == name]):
                r = x["pre_reserves"]
                spot = Fraction(r[0] * w[s.asset_in], r[s.asset_in] * w[0])  # asset_in in asset 0
                self.assertEqual(s.weight, s.lp_fee * spot)
                self.assertIsInstance(s.weight, Fraction)
                self.assertEqual(fee_weight(s.weight), s.weight)

    def test_xch_fee_is_its_mojos(self):
        first = walk(XCH_POOL).swaps[0]
        self.assertEqual((first.asset_in, first.lp_fee, first.weight), (0, 3_000_000, 3_000_000))
        self.assertEqual(lp_fee(1_000_000_000, 30), 3_000_000)
        self.assertEqual(lp_fee(3_333_333, 30), 10_000)  # the fee keeps the curve's floor

    def test_a_weighted_cat_pool_prices_in_asset_0(self):
        # 3:1 pool, reserves 30M:5M; 4,000 of asset 1 = 4000 * (30M/3)/(5M/1) = 8,000 of asset 0
        self.assertEqual(walk(CAT_POOL).swaps[0].weight, 8000)

    def test_a_leaf_run_twice_is_two_swaps(self):
        swaps = walk(XCH_POOL).swaps
        same_block = [s for s in swaps if s.height == swaps[2].height]
        self.assertEqual([(s.asset_in, s.gross_input) for s in same_block], [(0, 250_000_000), (1, 3_333_333)])

    def test_spends_without_a_swap_pay_no_fee(self):
        w = walk(XCH_POOL)
        self.assertEqual((w.spends, len(w.swaps)), (5, 5))  # one spend is observe only, two swaps share one
        ran = []
        for spend in FIXTURE["spends"].values():
            try:
                names, _ = parse_pool_spend(XCH_POOL, h(spend["puzzle_reveal"]), h(spend["solution"]), 1)
            except FeeReadError:  # the other pool's
                continue
            ran.append(names)
        self.assertEqual(sorted(ran), sorted([["forge_action_swap"], ["forge_action_swap"], ["forge_action_observe"],
                                              ["forge_action_swap", "forge_action_swap"],
                                              ["forge_action_swap", "forge_action_collect"]]))

    def test_events_are_half_open(self):
        w = walk(XCH_POOL)
        first, last = w.swaps[0].height, w.swaps[-1].height
        self.assertEqual(len(w.events(first, last)), 4)
        self.assertEqual(len(w.events(first, last + 1)), 5)
        self.assertEqual(w.events(first + 1, first + 2), [])

    def test_the_walk_is_incremental_and_survives_its_cache(self):
        node = RecordedNode()
        w = PoolFeeWalk(XCH_POOL)
        self.assertEqual(w.update(node), 5)
        again = PoolFeeWalk.from_json(json.loads(json.dumps(w.to_json())))
        self.assertEqual(again.swaps, w.swaps)
        node.calls.clear()
        self.assertEqual(again.update(node), 0)
        self.assertEqual(node.calls, ["get_coin_record_by_name"])  # only the tip is asked about
        stale = dict(w.to_json(), forge_puzzles="0" * 40)
        self.assertEqual(PoolFeeWalk.from_json(stale).swaps, [])  # read under other pins: start over

    def test_another_singletons_spend_is_refused(self):
        spend = next(iter(FIXTURE["spends"].values()))
        with self.assertRaises(FeeReadError):
            parse_pool_spend(b"\x99" * 32, h(spend["puzzle_reveal"]), h(spend["solution"]), 1)

    def test_a_reveal_that_is_not_the_coins_is_refused(self):
        data = json.loads(json.dumps(FIXTURE))
        spends = list(data["spends"].values())
        spends[0]["puzzle_reveal"], spends[1]["puzzle_reveal"] = spends[1]["puzzle_reveal"], spends[0]["puzzle_reveal"]
        with self.assertRaises(FeeReadError):
            PoolFeeWalk(XCH_POOL).update(RecordedNode(data))

    def test_a_selector_count_mismatch_is_refused(self):
        clvm = Clvm()
        spend = next(iter(FIXTURE["spends"].values()))
        sol = clvm.deserialize(h(spend["solution"])).to_list()
        puzzles, selectors, solutions = sol[2].to_list()[:3]
        bad = clvm.list([sol[0], sol[1], clvm.list([puzzles, selectors, clvm.list([*solutions.to_list()] * 2)])])
        with self.assertRaises(FeeReadError):
            parse_pool_spend(XCH_POOL, h(spend["puzzle_reveal"]), bad.serialize(), 1)


class TestProtocolScope(unittest.TestCase):
    """Only Forge V16 (protocol 17) pools are read."""

    def test_the_lp_asset_proves_the_protocol(self):
        for pool in POOLS.values():
            self.assertEqual(v16_lp_asset_id(h(pool["launcher_id"])), h(pool["lp_asset_id"]))
            check_protocol(h(pool["launcher_id"]), h(pool["lp_asset_id"]), 17)

    def test_older_and_unknown_protocols_are_refused(self):
        lp = h(POOLS["xch-cat"]["lp_asset_id"])
        for old in (16, 15):
            with self.assertRaisesRegex(UnsupportedPool, "only Forge V16"):
                check_protocol(XCH_POOL, lp, old)
        with self.assertRaisesRegex(UnsupportedPool, "no fee reader is pinned"):
            check_protocol(XCH_POOL, lp, 18)
        with self.assertRaisesRegex(UnsupportedPool, "not the V16 LP TAIL"):
            check_protocol(XCH_POOL, b"\x42" * 32)  # a V15 pool's LP, whatever the responder says

    def test_responder_versions_are_read(self):
        data = {"pools": [{"pool": {"launcherCoinId": XCH_POOL.hex(), "lpCatAssetId": POOLS["xch-cat"]["lp_asset_id"],
                                    "poolVersion": 17}},
                          {"pool": {"launcherCoinId": "ab" * 32, "lpCatAssetId": "cd" * 32, "poolVersion": 16}}]}
        lp_assets, versions = parse_forge_pools(data)
        self.assertEqual(versions, {XCH_POOL: 17, b"\xab" * 32: 16})
        src = LiveLpSource(lp_assets, RecordedNode(), versions=versions)
        self.assertEqual(src.lp_asset(XCH_POOL), h(POOLS["xch-cat"]["lp_asset_id"]))
        with self.assertRaises(UnsupportedPool):
            src.lp_asset(b"\xab" * 32)
        with self.assertRaises(UnsupportedPool):
            src.fee_events(b"\xab" * 32, 0, 10**7)


def live(cache=None):
    lp_assets = {h(p["launcher_id"]): h(p["lp_asset_id"]) for p in FIXTURE["pools"]}
    return LiveLpSource(lp_assets, RecordedNode(), cache, versions={k: 17 for k in lp_assets})


class TestLiveSource(unittest.TestCase):
    def test_fee_events_are_served(self):
        events = live().fee_events(XCH_POOL, 0, 10**7)
        self.assertEqual(events, [(s.height, s.weight) for s in walk(XCH_POOL).swaps])

    def test_the_cache_keeps_fee_walks_and_reads_the_old_format(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "lp-crawl-test.json"
            src = live(path)
            src.refresh_fees([XCH_POOL])
            again = LiveLpSource(src.lp_assets, RecordedNode(), path, versions=src.versions)
            again.load_cache()
            self.assertEqual(again.fee_walks[XCH_POOL].swaps, src.fee_walks[XCH_POOL].swaps)
            path.write_text(json.dumps({}))  # the first format: {asset: crawl}
            old = LiveLpSource(src.lp_assets, RecordedNode(), path)
            old.load_cache()
            self.assertEqual(old.fee_walks, {})

    def test_an_lp_campaign_scores_the_recorded_swaps(self):
        """End to end: holders from a fixture, fees from the recorded V16 spends.
        Bob joins in the block of the first swap and leaves after the last, so
        he takes his time share but nothing of the first swap's fee."""
        swaps = walk(XCH_POOL).swaps
        lp = h(POOLS["xch-cat"]["lp_asset_id"])
        alice, bob = b"\xa1" * 32, b"\xb2" * 32
        first = swaps[0].height
        holders = FixtureSource(events={lp: [HolderEvent(first - 300, alice, 100), HolderEvent(first, bob, 100)]})
        fees = live()
        joined = SimpleNamespace(lp_asset=fees.lp_asset, fee_events=fees.fee_events, excluded=fees.excluded,
                                 holder_events=holders.holder_events, nft_events=holders.nft_events,
                                 links=holders.links, forwarders=holders.forwarders)
        start, end = first - 100, first + 100
        c = Campaign(b"\xd1" * 32, b"\xcc" * 32, bytes(32), 10**9, start, end, Kind.LP_HOLD, XCH_POOL,
                     Params(time_bps=5000, fees_bps=5000, fee_window=200), fee_bps=0)
        # Time half: Alice alone for 100 blocks (250M), then half each (125M each).
        # Fee half: one bucket, [start, end). With only the first swap, its lookback
        # [first - 200, first) is Alice's alone: all 500M are hers, none Bob's.
        self.assertEqual(accrue(c, _only(joined, first), start, end), {alice: 875 * 10**6, bob: 125 * 10**6})
        # With every swap, Bob's later blocks count, a little.
        credit = accrue(c, joined, start, end)
        self.assertGreater(credit[bob], 125 * 10**6)
        self.assertLess(credit[bob], 125 * 10**6 + 5 * 10**8 * Fraction(12, 200))
        self.assertEqual(sum(credit.values()), 10**9)
        state, root = run_epoch(EngineState(b"\xd1" * 32, height=start), [c], joined, end)
        self.assertEqual(state.accrued[c.id], credit)


def _only(src, height):
    """`src` with fee events at `height` alone."""
    class Only:
        def __getattr__(self, name):
            return getattr(src, name)

        def fee_events(self, pool, start, end):
            return [(x, w) for x, w in src.fee_events(pool, start, end) if x == height]
    return Only()


if __name__ == "__main__":
    unittest.main()
