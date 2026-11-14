#!/usr/bin/env python3
"""Record `fixtures/v17_pool_spends.json`: real Forge V17 (protocol 18) pool spends, read from
testnet11, for the fee reader's V17 tests.

Unlike the V16 fixture (built offline), these are the chain's own answers: every call the
reader makes while walking each pool from its launcher (`get_coin_record_by_name`,
`get_coin_records_by_parent_ids`, `get_puzzle_and_solution`) is recorded as coinset.org
answered it, so the test walks the same bytes through the same `Rpc`. Only the pool coins the
walk reads are kept. Not part of the suite; needs network access:

    .venv/Scripts/python.exe scripts/record_v17_fee_fixture.py [--node URL]

The pools are two of Forge's V17 testnet11 pools (Forge `.awizard/v17-testnet.json`):
an XCH/rCAT pool and the three-asset DAO-fee pool.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flpcr.lp_crawl import node_rpc  # noqa: E402
from flpcr.v16_fees import FORGE_V17_COMMIT, PoolFeeWalk, check_protocol  # noqa: E402

OUT = ROOT / "fixtures" / "v17_pool_spends.json"
POOLS = [
    {"name": "xch-rcat", "launcher_id": "89d4c447b4dc440c485c1825e0beaa0ce5f8596e41ec95daa0935e84212622fc",
     "lp_asset_id": "17a63dfc643274a44f9267d0b2809e89fd895cb26dd1151edaf50d352fd989e7", "fee_bps": 30},
    {"name": "dao-3", "launcher_id": "58a35068e8cd876e28c976c65103e87def79e69e2e09aa2ac7b71e2f494f2fc6",
     "lp_asset_id": "3ec327adace7522faadadb724191a75cabe114b2d1358155484dc1e074481b31", "fee_bps": 30},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default="https://testnet11.api.coinset.org")
    args = ap.parse_args()
    live = node_rpc(args.node)
    records, spends = {}, {}

    def rpc(endpoint, body):
        out = live(endpoint, body)
        if endpoint == "get_coin_record_by_name" and out.get("coin_record"):
            records[body["name"]] = out["coin_record"]
        elif endpoint == "get_coin_records_by_parent_ids":
            for r in out.get("coin_records") or []:
                if int(r["coin"]["amount"]) % 2 == 1:  # the walk follows odd children only
                    records.setdefault(r["coin"]["parent_coin_info"] + ":" + r["coin"]["puzzle_hash"], r)
        elif endpoint == "get_puzzle_and_solution":
            spends[body["coin_id"].removeprefix("0x")] = out["coin_solution"]
        return out

    expected = []
    for pool in POOLS:
        launcher = bytes.fromhex(pool["launcher_id"])
        pool["protocol"] = check_protocol(launcher, bytes.fromhex(pool["lp_asset_id"]))
        walk = PoolFeeWalk(launcher, pool["protocol"])
        walk.update(rpc)
        pool["spends"] = walk.spends
        expected += [{"pool": pool["name"], "height": s.height, "asset_in": s.asset_in, "asset_out": s.asset_out,
                      "gross_input": s.gross_input, "lp_fee": s.lp_fee,
                      "weight": [s.weight.numerator, s.weight.denominator]} for s in walk.swaps]
        print(f"{pool['name']}: protocol {pool['protocol']}, {walk.spends} spends, {len(walk.swaps)} swaps")
    OUT.write_text(json.dumps({
        "provenance": f"Forge V17 pool spends on testnet11, recorded from {args.node} by "
                      "scripts/record_v17_fee_fixture.py (the node's own answers).",
        "forge_v17_commit": FORGE_V17_COMMIT,
        "pools": POOLS,
        "coin_records": list(records.values()),
        "spends": spends,
        "expected_swaps": expected,
    }, indent=1) + "\n", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(records)} coin records, {len(spends)} spends")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
