"""Build one root from a fixture.

    python -m flpcr fixtures/demo.json --end 2000 [--state state.json] [--link-log links.json]

Prints the root manifest, or `{"posted": false, ...}` when nothing claimable
changed (the next run covers the longer window). With --state, reads the prior
engine state from that file if it exists and writes the new state back. With
--link-log, writes the link log prefix the root commits, the file to publish
next to the manifest.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .engine import EngineState, run_epoch
from .links import log_to_json
from .manifest import manifest_hash, root_to_json, state_from_json, state_to_json
from .model import DEFAULT_FEE_WINDOW, Boost, Campaign, Holding, Kind, Params, Requirement
from .sources import FixtureSource


def requirement_from_json(r: dict) -> Requirement:
    return Requirement(Holding[r["holding"]], bytes.fromhex(r["asset"]), r.get("min_count", 1))


def campaign_from_json(d: dict) -> Campaign:
    h = bytes.fromhex
    p = d.get("params", {})
    return Campaign(
        distributor_id=h(d["distributor_id"]),
        creator=h(d["creator"]),
        reward_asset=h(d["reward_asset"]),
        amount=d["amount"],
        start=d["start"],
        end=d["end"],
        kind=Kind[d["kind"]],
        target=h(d["target"]),
        params=Params(
            time_bps=p.get("time_bps", 10_000),
            fees_bps=p.get("fees_bps", 0),
            min_balance=p.get("min_balance", 0),
            blacklist=frozenset(h(x) for x in p.get("blacklist", [])),
            whitelist=frozenset(h(x) for x in p.get("whitelist", [])),
            forward=p.get("forward", True),
            gates=tuple(requirement_from_json(g) for g in p.get("gates", [])),
            boosts=tuple(Boost(requirement_from_json(b), b["bps"]) for b in p.get("boosts", [])),
            boost_cap_bps=p.get("boost_cap_bps", 25_000),
            fee_window=p.get("fee_window", DEFAULT_FEE_WINDOW),
        ),
        nonce=d.get("nonce", 0),
        **{k: d[k] for k in ("fee_bps", "claim_window") if k in d},
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="flpcr")
    ap.add_argument("fixture")
    ap.add_argument("--end", type=int, required=True, help="accrue up to this height (exclusive)")
    ap.add_argument("--state", help="engine state file, read if present and rewritten")
    ap.add_argument("--link-log", help="write the link log prefix the root commits to this file")
    args = ap.parse_args(argv)

    with open(args.fixture, encoding="utf-8") as f:
        data = json.load(f)
    source = FixtureSource.from_json(data)
    campaigns = [campaign_from_json(c) for c in data["campaigns"]]
    distributor = campaigns[0].distributor_id

    if args.state and os.path.exists(args.state):
        with open(args.state, encoding="utf-8") as f:
            state = state_from_json(json.load(f))
    else:
        state = EngineState(distributor)

    state, root = run_epoch(state, campaigns, source, args.end)
    if root.root is None:
        out = {"posted": False, "distributor_id": distributor.hex(), "end": args.end}
    else:
        out = root_to_json(root)
        out["manifest_hash"] = manifest_hash(root).hex()
        if args.link_log:
            with open(args.link_log, "w", encoding="utf-8") as f:
                json.dump(log_to_json(source.links()[: root.link_count]), f, indent=2)
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")

    if args.state:
        with open(args.state, "w", encoding="utf-8") as f:
            json.dump(state_to_json(state), f, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
