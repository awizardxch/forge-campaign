"""Shared helpers for reviewer A's probes. Run with cwd=<repo>."""
import sys
sys.path.insert(0, __import__('pathlib').Path(__file__).resolve().parents[4].as_posix())

from flpcr.chain.sim import SimWorld
from flpcr.chain.driver import (AUTH_DELEGATE, AUTH_MESSAGE, AUTH_SIGNATURE, Action, Distributor,
                                send_message_condition)
from flpcr.clvm import sha256tree
from flpcr.engine import Claim, Root
from flpcr.merkle import build_tree, claim_leaf
from flpcr import EngineState, FixtureSource, HolderEvent, Kind, run_epoch

POOL, LP = b"\xb0" * 32, b"\x1b" * 32
WINDOW = 16

RESULTS = []


def attempt(name, fn, *a, expect="accept", **kw):
    try:
        out = fn(*a, **kw)
        verdict = "ACCEPTED"
        detail = repr(out)
    except Exception as e:  # noqa
        verdict = "REFUSED"
        detail = str(e).splitlines()[0][:200]
    ok = (verdict == "ACCEPTED") == (expect == "accept")
    line = f"[{'as-expected' if ok else 'UNEXPECTED'}] {name}: {verdict} ({detail})"
    print(line, flush=True)
    RESULTS.append((name, verdict, detail, ok))
    return verdict == "ACCEPTED"


def world(asset=bytes(32), holders=None):
    w = SimWorld(reward_asset=asset)
    w.advance_to(w.h + 3)
    camp = w.campaign("creator", 1_000_000, w.h + 2, w.h + 42, Kind.LP_HOLD, POOL, claim_window=WINDOW)
    return w, camp


def make_root(w, root_id, start, end, entries):
    """entries: list of (campaign_id, ph, cumulative). A hand-built root, as an operator would post."""
    leaves = [claim_leaf(w.launcher_id, c, ph, cum) for c, ph, cum in entries]
    root, proofs = build_tree(leaves)
    totals = {}
    claims = []
    for (c, ph, cum), leaf in zip(entries, leaves):
        totals[c] = totals.get(c, 0) + cum
        claims.append(Claim(c, ph, cum, leaf, proofs[leaf]))
    return Root(w.launcher_id, root_id, start, end, root, totals, claims)


def engine_root(w, camp, src, end):
    return run_epoch(EngineState(w.launcher_id, height=camp.start), [camp], src, end)
