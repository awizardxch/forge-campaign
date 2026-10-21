#!/usr/bin/env python3
"""The operator, for one campaign on testnet11: post a root every so often and
the one covering the campaign's end right after it, then stop.

    python scripts/operator_testnet.py --every 180      # blocks between roots (~56 min)

A root is posted when no root is pending, the campaign has started, and either
`--every` blocks have passed since the last root's end or the campaign has
ended and the last root does not reach its end. Everything else (claims,
reclaim, delegation) stays manual in deploy_testnet.py.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_testnet as dep  # noqa: E402
from chia_wallet_sdk import Clvm  # noqa: E402
from flpcr.chain.driver import Puzzles  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--every", type=int, default=180, help="blocks between roots while the campaign runs")
    p.add_argument("--poll", type=int, default=120, help="seconds between checks")
    p.add_argument("--source", choices=("fixture", "live"), default="fixture",
                   help="live: every real LP holder of each target pool, crawled from the chain")
    args = p.parse_args()
    clvm = Clvm()
    puzzles = Puzzles(clvm)
    wallet = dep.Wallet(clvm)
    while True:
        state = dep.load()
        d = dep.distributor_from(state, puzzles)
        live = [dep.campaign_from(state, d, k.hex()) for k in d.chain_state.by_id if k.hex() in dep.campaign_records(state)]
        if not live:
            print("no live campaign; done", flush=True)
            return 0
        start, end = min(c.start for c in live), max(c.end for c in live)
        st = d.chain_state
        h = dep.peak()
        last_end = max((int(r["end"]) for r in state.get("roots", {}).values()), default=start)
        pending = st.pending is not None and h < st.pending.activates_at
        if last_end >= end:
            print(f"final root posted (end {last_end}); done", flush=True)
            return 0
        due = h > start and not pending and (h - last_end >= args.every or h >= end)
        print(f"peak {h}  campaigns {start}-{end} ({len(live)})  last root end {last_end}  pending {pending}  "
              f"{'posting' if due else 'waiting'}", flush=True)
        if due:
            class A:
                end = None
                source = args.source
            try:
                dep.cmd_post(A(), wallet, state, puzzles)
            except SystemExit as e:
                print(f"post failed: {e}", flush=True)
        time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
