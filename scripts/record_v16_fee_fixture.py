#!/usr/bin/env python3
"""Record `fixtures/v16_pool_spends.json`: real Forge V16 pool spends, built
offline with the forge-puzzles driver, for the fee-event reader's tests.

Not part of the engine and not run by the suite. It needs what the engine does
not: chia-blockchain (`pip install "chia-blockchain>=2.3.0,<3"`) and a checkout
of awizardxch/forge-puzzles at the commit `flpcr.v16_fees.FORGE_PUZZLES_COMMIT`
names (default `../awizardxch/forge-puzzles`, or `FORGE_PUZZLES`):

    FORGE_PUZZLES=/path/to/forge-puzzles python scripts/record_v16_fee_fixture.py

Every bundle is assembled by `forge_v16_driver.spend_actions` (the module the
Forge deploy uses) and passed through `validate` (chia_rs's mempool validator)
before it is recorded. Coins are fabricated, as in the forge-puzzles suites,
and heights are assigned as a chain would: a spend claiming height `h` is
included at `h + 1`, where its successor is born.

What is written is what a full node answers (`get_coin_records_by_parent_ids`,
`get_coin_record_by_name`, `get_puzzle_and_solution`), so the reader under test
walks the fixture through the same `Rpc` it uses live, plus the swaps the driver
was asked to build, as the reader's expected output.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FP = pathlib.Path(os.environ.get("FORGE_PUZZLES") or ROOT.parent / "awizardxch" / "forge-puzzles").resolve()
CONTRACTS = FP / "contracts"
os.chdir(CONTRACTS)
sys.path.insert(0, str(CONTRACTS))

from chia.types.blockchain_format.program import Program  # noqa: E402
from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER_HASH  # noqa: E402
from chia_rs.sized_bytes import bytes32  # noqa: E402

import forge_math  # noqa: E402
import forge_v16_driver as drv  # noqa: E402

OUT = ROOT / "fixtures" / "v16_pool_spends.json"
H = 6_999_966  # the driver validates at 7,000,000; the prologue wants h within its oracle window


def coin_json(coin) -> dict:
    return {"parent_coin_info": "0x" + bytes(coin.parent_coin_info).hex(),
            "puzzle_hash": "0x" + bytes(coin.puzzle_hash).hex(), "amount": int(coin.amount)}


def record(coin, confirmed: int, spent: int = 0) -> dict:
    return {"coin": coin_json(coin), "confirmed_block_index": confirmed, "spent": bool(spent),
            "spent_block_index": spent, "coinbase": False, "timestamp": 0}


class Recorder:
    def __init__(self):
        self.records, self.spends, self.pools, self.swaps = {}, {}, [], []

    def add(self, coin, confirmed, spent=0):
        self.records[bytes(coin.name()).hex()] = record(coin, confirmed, spent)


def swap_step(pool, state, eph, asset_in, asset_out, gross, h, salt, extra_spends, extra_cats, expected):
    r, w = state[0], pool.weights
    out = forge_math.swap_output(r[asset_in], r[asset_out], gross, pool.fee_bps, w[asset_in], w[asset_out])
    asset = pool.asset_ids[asset_in]
    if asset is None:
        coin, spend = drv.offer_settlement_xch(gross, salt=salt)
        extra_spends.append(spend)
    else:
        coin, spendable = drv.offer_settlement_cat(asset, gross, salt=salt)
        extra_cats.setdefault(asset, []).append(spendable)
    lp_fee = gross - gross * (10_000 - pool.fee_bps) // 10_000
    expected.append({"asset_in": asset_in, "asset_out": asset_out, "gross_input": gross, "claimed_output": out,
                     "lp_fee": lp_fee})
    solution = [h, asset_in, asset_out, gross, out, *drv.settlement_ref(coin)]
    st, _, _, eph = drv.run_leaf(pool, "forge_action_swap", solution, ephemeral=eph, state=state)
    return ("forge_action_swap", solution), drv.state_to_list(st), eph


def run_pool(rec: Recorder, name: str, pool, plan) -> None:
    """`plan`: one entry per spend, a list of ("swap", asset_in, asset_out, gross) / ("observe",) / ("collect", i)."""
    launcher = drv.Coin(pool.launcher_parent, SINGLETON_LAUNCHER_HASH, 1)
    assert bytes(launcher.name()) == bytes(pool.launcher_id)
    born = pool.birth
    rec.add(launcher, born, born)
    rec.pools.append({"name": name, "launcher_id": bytes(pool.launcher_id).hex(), "lp_asset_id": bytes(pool.lp_asset_id).hex(),
                      "asset_ids": [bytes(a or bytes(32)).hex() for a in pool.asset_ids],
                      "weights": list(pool.weights), "fee_bps": pool.fee_bps})
    h = born
    for k, actions in enumerate(plan):
        h += 3
        steps, extra_spends, extra_cats, expected = [], [], {}, []
        state, eph = pool.state, None
        pre = list(state[0])
        for j, a in enumerate(actions):
            if a[0] == "swap":
                step, state, eph = swap_step(pool, state, eph, a[1], a[2], a[3], h, 0x40 + 16 * k + j, extra_spends, extra_cats, expected)
                steps.append(step)
            else:
                step = ("forge_action_observe", [h]) if a[0] == "observe" else ("forge_action_collect", [h, [a[1]]])
                st, _, _, eph = drv.run_leaf(pool, step[0], step[1], ephemeral=eph, state=state)
                state = drv.state_to_list(st)
                steps.append(step)
        bundle, new_state = drv.spend_actions(pool, steps, extra_spends=extra_spends, extra_cats=extra_cats)
        drv.validate(bundle)
        spend = next(cs for cs in bundle.coin_spends if cs.coin.name() == pool.coin.name())
        included = h + 1
        rec.add(pool.coin, born, included)
        rec.spends[bytes(pool.coin.name()).hex()] = {
            "coin": coin_json(pool.coin), "puzzle_reveal": "0x" + bytes(spend.puzzle_reveal).hex(),
            "solution": "0x" + bytes(spend.solution).hex()}
        for child in bundle.additions():
            if child.parent_coin_info == pool.coin.name():
                rec.add(child, included)
        for e in expected:
            rec.swaps.append({"pool": name, "height": included, "pre_reserves": pre, **e})
        pool = pool.advance(new_state)
        pool = drv.replace(pool, birth=included)
        born = included
        assert bytes(pool.coin.name()).hex() in rec.records, "the successor is a coin the spend created"


def main() -> int:
    if not drv.v16_available():
        raise SystemExit(f"no V16 build in {CONTRACTS}")
    commit = subprocess.run(["git", "-C", str(FP), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    rec = Recorder()
    cat_a, cat_b = bytes32(b"\xca" * 32), bytes32(b"\xcb" * 32)
    # An XCH pool, 50/50: XCH in, CAT in, a quiet spend, two swaps in one spend, a swap and a collect.
    xch = drv.make_pool([None, cat_a], [40_000_000_000, 90_000_000], total_lp=60_000_000, leaves="forge",
                        salt=0x61, fee_bps=30, protocol_fee_bps=5, last_height=H - 1)
    run_pool(rec, "xch-cat", xch, [
        [("swap", 0, 1, 1_000_000_000)],
        [("swap", 1, 0, 7_000_000)],
        [("observe",)],
        [("swap", 0, 1, 250_000_000), ("swap", 1, 0, 3_333_333)],
        [("swap", 1, 0, 1_234_567), ("collect", 0)],
    ])
    # A CAT/CAT pool weighted 3:1 (no XCH: fees are valued in asset 0).
    cats = drv.make_pool([cat_a, cat_b], [30_000_000, 5_000_000], total_lp=10_000_000, leaves="forge",
                         salt=0x62, weights=[3, 1], fee_bps=100, protocol_fee_bps=5, last_height=H - 1)
    run_pool(rec, "cat-cat-3-1", cats, [
        [("swap", 1, 0, 400_000)],
        [("swap", 0, 1, 2_000_000)],
    ])
    out = {
        "provenance": ("Real Forge V16 pool spends assembled offline by forge_v16_driver.spend_actions and accepted by "
                       "its validate (chia_rs mempool validator); coins and heights are fabricated. Written by "
                       "scripts/record_v16_fee_fixture.py."),
        "forge_puzzles_commit": commit,
        "pools": rec.pools,
        "coin_records": list(rec.records.values()),
        "spends": rec.spends,
        "expected_swaps": rec.swaps,
    }
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(rec.spends)} pool spends, {len(rec.swaps)} swaps (forge-puzzles {commit[:7]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
