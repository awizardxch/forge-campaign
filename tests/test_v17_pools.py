"""Forge V17 (protocol 18) pools as campaign targets: their fee events, read from real testnet11
spends (`fixtures/v17_pool_spends.json`, the node's own answers), their LP asset proving the
revision, and an LP campaign scored on them.

V17 pools run eight leaves on the same action layer (V16's six plus the rCAT `rebase` and
`refund`); `xch-rcat` holds an rCAT reserve (CHIP-0038, Forge's Reading C: the reserve stays
under the revocation layer, the LP stays a plain CAT), `dao-3` is the three-asset DAO-fee pool.
"""

import json
import pathlib
import unittest
from fractions import Fraction
from types import SimpleNamespace

from chia_wallet_sdk import Clvm, Coin

from flpcr import Campaign, EngineState, FixtureSource, HolderEvent, Kind, Params, accrue, run_epoch
from flpcr.live_source import LiveLpSource, parse_forge_pools
from flpcr.lp_crawl import CUSTODY, LP_MINT_INNER_HASH
from flpcr.v16_fees import (
    FORGE_V17_COMMIT, PROTOCOL_V16, PROTOCOL_V17, V17_LEAF_MOD_HASHES, PoolFeeWalk, UnsupportedPool, check_protocol,
    lp_fee, parse_pool_spend, v17_lp_asset_id,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "fixtures" / "v17_pool_spends.json").read_text())
POOLS = {p["name"]: p for p in FIXTURE["pools"]}
RCAT_POOL = bytes.fromhex(POOLS["xch-rcat"]["launcher_id"])
DAO_POOL = bytes.fromhex(POOLS["dao-3"]["launcher_id"])


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


def walk(pool, protocol=PROTOCOL_V17):
    w = PoolFeeWalk(pool, protocol)
    w.update(RecordedNode())
    return w


class TestV17Fees(unittest.TestCase):
    def test_recorded_at_the_pinned_commit(self):
        self.assertEqual(FIXTURE["forge_v17_commit"], FORGE_V17_COMMIT)
        self.assertEqual(len(V17_LEAF_MOD_HASHES), 8)  # the eight-leaf V17 root

    def test_every_recorded_swap_is_read(self):
        for name, pool in POOLS.items():
            got = walk(h(pool["launcher_id"]))
            self.assertEqual(got.spends, pool["spends"])
            want = [x for x in FIXTURE["expected_swaps"] if x["pool"] == name]
            self.assertEqual([(s.height, s.asset_in, s.asset_out, s.gross_input, s.lp_fee) for s in got.swaps],
                             [(x["height"], x["asset_in"], x["asset_out"], x["gross_input"], x["lp_fee"]) for x in want])
            for s in got.swaps:
                self.assertEqual(s.lp_fee, lp_fee(s.gross_input, pool["fee_bps"]))
                if s.asset_in == 0:
                    self.assertEqual(s.weight, Fraction(s.lp_fee))  # XCH fees are their mojos

    def test_an_rcat_input_is_priced_in_xch(self):
        rcat_in = [s for s in walk(RCAT_POOL).swaps if s.asset_in == 1]
        self.assertTrue(rcat_in)
        self.assertTrue(all(s.weight > 0 for s in rcat_in))

    def test_a_v17_pool_read_as_v16_is_refused(self):
        # V18 shares V17's swap/observe/collect/dao_fee leaves, so the reader names both protocols
        with self.assertRaisesRegex(UnsupportedPool, r"protocol 18(/19)? leaf, not protocol 17"):
            walk(RCAT_POOL, PROTOCOL_V16)

    def test_a_quoted_selector_reads_the_same(self):
        """CE-13's sibling in the fee reader: `(q . leaf)` is a valid selector on chain."""
        clvm = Clvm()
        checked = 0
        for coin_id, spend in FIXTURE["spends"].items():
            sol = clvm.deserialize(h(spend["solution"])).to_list()
            puzzles, selectors, solutions = sol[2].to_list()[:3]
            quoted = []
            for entry in selectors.to_list():
                path, node = entry.first().to_int(), puzzles
                while path > 1:
                    node = node.rest() if path & 1 else node.first()
                    path >>= 1
                quoted.append(clvm.pair(clvm.pair(clvm.atom(b"\x01"), node), entry.rest()))
            inner = clvm.list([puzzles, clvm.list(quoted), solutions, *sol[2].to_list()[3:]])
            bad = clvm.list([sol[0], sol[1], inner]).serialize()
            for launcher in (RCAT_POOL, DAO_POOL):
                try:
                    honest = parse_pool_spend(launcher, h(spend["puzzle_reveal"]), h(spend["solution"]), 1)
                except Exception:  # noqa: BLE001 -- the other pool's spend
                    continue
                self.assertEqual(parse_pool_spend(launcher, h(spend["puzzle_reveal"]), bad, 1), honest)
                checked += 1
        self.assertEqual(checked, len(FIXTURE["spends"]))


class TestV18Pins(unittest.TestCase):
    """V18 (protocol 19), pinned from Forge contracts/v18/compiled: a real testnet11 V18 pool's LP asset
    (A2 txch T6 v18, launcher 256a9e8b..., LP d537a5eb...) derives under protocol 19 and no other, and
    the shared V17 leaves still read V17 pools as V17."""

    def test_a_real_v18_pool(self):
        import flpcr.v16_fees as fees
        launcher = h("256a9e8bf479365ac905563de5ab79143ae39b0a2c1776effb053f8744e7a673")
        lp = h("d537a5eb450c2816b2a2170adabd12b622b1fab5154492e15593b86c580e2279")
        self.assertEqual(fees.MAX_PROTOCOL, 19)
        self.assertEqual(fees.lp_asset_id(launcher, 19), lp)
        self.assertEqual(fees.check_protocol(launcher, lp), 19)
        self.assertEqual(fees.check_protocol(launcher, lp, 19), 19)
        with self.assertRaises(UnsupportedPool):
            fees.check_protocol(launcher, lp, PROTOCOL_V17)
        for pool in POOLS.values():
            self.assertEqual(fees.check_protocol(h(pool["launcher_id"]), h(pool["lp_asset_id"])), PROTOCOL_V17)


class TestNextRevision(unittest.TestCase):
    """V18 (protocol 19) is V17 plus a registry rule: same pool leaves, same LP TAIL mod. Adding it
    is one REVISIONS entry; shared leaf hashes must not confuse the two."""

    def test_a_revision_that_reuses_v17_pins(self):
        from unittest import mock
        import flpcr.v16_fees as fees
        revs = dict(fees.REVISIONS)
        revs[19] = fees.Revision("V18", 19, fees.V17_LP_TAIL_MOD_HASH, fees.V17_LEAF_MOD_HASHES, "hypothetical")
        with mock.patch.object(fees, "REVISIONS", revs), \
                mock.patch.object(fees, "LEAVES_BY_HASH", fees.leaf_index(revs)), \
                mock.patch.object(fees, "MAX_PROTOCOL", 19):
            for pool in POOLS.values():
                launcher, lp = h(pool["launcher_id"]), h(pool["lp_asset_id"])
                self.assertEqual(fees.check_protocol(launcher, lp), PROTOCOL_V17)  # the LP asset decides
                with self.assertRaisesRegex(UnsupportedPool, "not the V18 LP TAIL"):
                    fees.check_protocol(launcher, lp, 19)
                self.assertNotEqual(fees.lp_asset_id(launcher, 19), lp)
            self.assertEqual([s.lp_fee for s in walk(RCAT_POOL).swaps],
                             [x["lp_fee"] for x in FIXTURE["expected_swaps"] if x["pool"] == "xch-rcat"])
            w = PoolFeeWalk(RCAT_POOL, 19)  # shared leaves read under 19 too
            w.update(RecordedNode())
            self.assertEqual(len(w.swaps), 3)
            with self.assertRaisesRegex(UnsupportedPool, "not protocol 17"):
                walk(RCAT_POOL, PROTOCOL_V16)


class TestV17Scope(unittest.TestCase):
    def test_the_lp_asset_proves_v17(self):
        for pool in POOLS.values():
            self.assertEqual(v17_lp_asset_id(h(pool["launcher_id"])), h(pool["lp_asset_id"]))
            self.assertEqual(check_protocol(h(pool["launcher_id"]), h(pool["lp_asset_id"]), 18), PROTOCOL_V17)
            self.assertEqual(check_protocol(h(pool["launcher_id"]), h(pool["lp_asset_id"])), PROTOCOL_V17)
            with self.assertRaisesRegex(UnsupportedPool, "not the V16 LP TAIL"):
                check_protocol(h(pool["launcher_id"]), h(pool["lp_asset_id"]), 17)

    def test_v17_lp_is_minted_through_the_v16_custody(self):
        """V17 kept the LP mint/melt inner puzzles and the offer settlement: the crawl's starting
        puzzle hash and custody set are the same (Forge 350d023 forge_v17_driver)."""
        self.assertIn(LP_MINT_INNER_HASH, CUSTODY)

    def test_the_responder_serves_a_v17_pool(self):
        data = {"pools": [{"pool": {"launcherCoinId": RCAT_POOL.hex(), "lpCatAssetId": POOLS["xch-rcat"]["lp_asset_id"],
                                    "poolVersion": 18}}]}
        lp_assets, versions = parse_forge_pools(data)
        src = LiveLpSource(lp_assets, RecordedNode(), versions=versions)
        self.assertEqual(src.protocol(RCAT_POOL), PROTOCOL_V17)
        events = src.fee_events(RCAT_POOL, 0, 10**8)
        self.assertEqual(events, [(s.height, s.weight) for s in walk(RCAT_POOL).swaps])


class TestV17Campaign(unittest.TestCase):
    def test_an_lp_campaign_scores_the_recorded_v17_swaps(self):
        swaps = walk(RCAT_POOL).swaps
        lp = h(POOLS["xch-rcat"]["lp_asset_id"])
        alice, bob = b"\xa1" * 32, b"\xb2" * 32
        first = swaps[0].height
        holders = FixtureSource(events={lp: [HolderEvent(first - 300, alice, 100), HolderEvent(first, bob, 100)]})
        fees = LiveLpSource({RCAT_POOL: lp}, RecordedNode(), versions={RCAT_POOL: 18})
        joined = SimpleNamespace(lp_asset=fees.lp_asset, fee_events=fees.fee_events, excluded=fees.excluded,
                                 holder_events=holders.holder_events, nft_events=holders.nft_events,
                                 links=holders.links, forwarders=holders.forwarders)
        start, end = first - 100, first + 100
        c = Campaign(b"\xd1" * 32, b"\xcc" * 32, bytes(32), 10**9, start, end, Kind.LP_HOLD, RCAT_POOL,
                     Params(time_bps=5000, fees_bps=5000, fee_window=200), fee_bps=0)
        credit = accrue(c, joined, start, end)
        # time half: Alice alone 100 blocks (250M) then halves (125M each); fee half: the first
        # swap's lookback is Alice's alone, and every later swap in the window counts Bob too.
        self.assertEqual(sum(credit.values()), 10**9)
        self.assertGreater(credit[bob], 125 * 10**6)  # a later swap's lookback includes him
        self.assertGreater(credit[alice], credit[bob])
        state, root = run_epoch(EngineState(b"\xd1" * 32, height=start), [c], joined, end)
        self.assertEqual(state.accrued[c.id], credit)


if __name__ == "__main__":
    unittest.main()
