"""How many holders one campaign can serve: the engine, then every holder
claiming through the sequencer, on the simulator.

    python scripts/scale_claims.py --holders 1000

Every holder is a real BLS wallet and claims by key signature (no coin at
the address), the way a WalletConnect wallet signs for all its addresses at
once. The sequencer packs the claims into chained generations of the
distributor, one bundle per block, under the CLVM budget given.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flpcr import EngineState, FixtureSource, HolderEvent, Kind, run_epoch  # noqa: E402
from flpcr.chain.auth import BySignature  # noqa: E402
from flpcr.chain.driver import HEIGHT_WINDOW, ClaimRequest  # noqa: E402
from flpcr.chain.sim import SimWorld  # noqa: E402

POOL = bytes.fromhex("b0" * 32)
LP = bytes.fromhex("1b" * 32)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holders", type=int, default=1000)
    ap.add_argument("--events", type=int, default=4, help="balance changes per holder over the campaign")
    ap.add_argument("--max-cost", type=int, default=5_000_000_000, help="CLVM budget per bundle")
    ap.add_argument("--per-generation", type=int, default=25)
    args = ap.parse_args()

    w = SimWorld()
    w.advance_to(w.h + 3)
    t = time.time()
    wallets = [w.bls() for _ in range(args.holders)]
    print(f"{args.holders} wallets in {time.time() - t:.1f}s")
    start = w.h + HEIGHT_WINDOW + 2
    length = 4608  # a day of blocks
    c = w.campaign("creator", 10**12, start, start + length, Kind.LP_HOLD, POOL, claim_window=3 * w.cfg.dispute_blocks + HEIGHT_WINDOW + 64)
    events = []
    for i, wallet in enumerate(wallets):
        events.append(HolderEvent(0, wallet.puzzle_hash, 4 + i % 7))
        for k in range(1, args.events):  # moves in and out across the day
            events.append(HolderEvent(start + (k * length) // args.events + i % 97, wallet.puzzle_hash, (-1) ** k * (1 + i % 3)))
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: events})
    w.create(c, "creator")
    w.advance_to(c.end + 1)

    t = time.time()
    _, root = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.end)
    engine_s = time.time() - t
    depth = len(root.claim_for(c.id, wallets[0].puzzle_hash).proof[1])
    print(f"engine: {len(events):,} events, {len(root.claims):,} leaves, proof depth {depth}, {engine_s:.2f}s")
    w.post(root)
    w.advance_to(w.h + HEIGHT_WINDOW + w.cfg.dispute_blocks)

    by_ph = {x.puzzle_hash: x for x in wallets}
    queue = [ClaimRequest(c.id, cl.puzzle_hash, cl.cumulative, cl.proof, BySignature(by_ph[cl.puzzle_hash].pk.to_bytes()))
             for cl in root.claims if cl.campaign_id == c.id]
    owed = sum(r.cumulative for r in queue)
    paid_before = sum(w.balance(x.puzzle_hash) for x in wallets)
    bundles = gens = 0
    total_cost = 0
    t = time.time()
    while queue:
        batch = w.d.sequence(w.h, queue, max_cost=args.max_cost, per_generation=args.per_generation)
        if batch.rejected:
            raise SystemExit(f"rejected: {batch.rejected[:3]}")
        if not batch.generations:
            raise SystemExit("nothing placed")
        keys = [by_ph[r.puzzle_hash].sk for r in batch.placed]
        w.sim.spend_coins(batch.spends, keys)
        w.d.apply_batch(batch)
        w.d = w.d.reloaded()  # a fresh allocator per bundle, as a long-running sequencer does
        bundles += 1
        gens += len(batch.generations)
        total_cost += batch.cost
        print(f"  bundle {bundles}: {len(batch.placed)} claims in {len(batch.generations)} generations, "
              f"CLVM {batch.cost:,} ({batch.cost // max(1, len(batch.placed)):,}/claim), {len(batch.spends)} spends")
        queue = batch.deferred
        w.sim.create_block()
    paid = sum(w.balance(x.puzzle_hash) for x in wallets) - paid_before
    print(f"claims: {args.holders} in {bundles} bundles / {gens} generations, {time.time() - t:.1f}s to build; "
          f"paid {paid:,} of {owed:,} owed; reserve left {w.reserve_on_chain():,}")
    print(f"average CLVM per claim {total_cost // args.holders:,}; {bundles} bundle(s): at one per transaction "
          f"block (~52 s) every holder is paid within ~{bundles * 52 / 60:.0f} min")
    assert paid == owed, "every holder must be paid exactly what the root says"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
