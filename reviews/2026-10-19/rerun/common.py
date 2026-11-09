"""Shared helpers for the 2026-10-19 review rerun against the ten-leaf build.

Run any script from the repo root:

    PYTHONPATH=.:reviews/2026-10-19/rerun python reviews/2026-10-19/rerun/r02_ungranted_delegate.py

Every check is one `attempt`: an honest control that must be ACCEPTED, or an
attack that must be REFUSED for a named reason. A puzzle refusal surfaces as
`clvm raise` (the driver runs the leaf locally before any node sees it) and
carries no rule, so each script says in a comment which assertion is the first
that can fire and why. A consensus refusal names its rule, and the attempt
pins that name. A script exits non-zero if any attempt went the other way.
"""

from __future__ import annotations

import re
import sys
import traceback

from chia_wallet_sdk import Coin, CoinSpend  # noqa: F401  (re-exported for the scripts)

from flpcr import EngineState, FixtureSource, HolderEvent, Kind, run_epoch
from flpcr.chain.auth import ByAdmin, ByDelegate, ByMessage, BySignature, Message  # noqa: F401
from flpcr.chain.driver import (  # noqa: F401
    HEIGHT_WINDOW, LOW, HIGH, Action, BundleCollision, DelegationRecord, Distributor, LeafCall,
)
from flpcr.chain.sim import SimWorld
from flpcr.chain.values import to_program  # noqa: F401

POOL, LP = b"\xb0" * 32, b"\x1b" * 32
CAT_ASSET = b"\xa5" * 32
ASSETS = (("XCH", bytes(32)), ("CAT", CAT_ASSET))
WINDOW = 160  # the testnet claim-window floor: 4 * (dispute_blocks + HEIGHT_WINDOW)
LEAD = 32  # a campaign starts at least HEIGHT_WINDOW blocks after its create's `h`
CLVM_RAISE = "clvm raise"

RESULTS = []


def reason_of(message: str) -> str:
    if "clvm raise" in message:
        return CLVM_RAISE
    m = re.search(r"([A-Z][A-Za-z]+(?:Failed|NotSentOrReceived|Spend|Output|Unspent|Negative|Hash|Fee|Error))",
                  message)
    return m.group(1) if m else message.splitlines()[0][:120]


def attempt(name: str, fn, *args, expect: str = "accept", reason: str = None, **kwargs):
    """Run `fn`. expect="accept": must not raise. expect="refuse": must raise
    a ValueError (a leaf `clvm raise` or a consensus `Simulator error`, never a
    Python bug), whose message contains `reason` when given."""
    full = ""
    try:
        out = fn(*args, **kwargs)
        verdict, detail = "ACCEPTED", repr(out) if isinstance(out, (int, str, bool, tuple)) else ""
    except BundleCollision as e:
        out, verdict, detail = None, "REFUSED", f"BundleCollision: {e}"
    except ValueError as e:
        out, verdict, detail = None, "REFUSED", reason_of(str(e))
        full = str(e)
    except Exception as e:  # a Python error is a bug, never a refusal
        traceback.print_exc()
        out, verdict, detail = None, "ERROR", f"{type(e).__name__}: {e}"
    if expect == "accept":
        ok = verdict == "ACCEPTED"
    else:
        ok = verdict == "REFUSED" and (reason is None or reason in detail or reason in full)
        if reason and reason not in detail and reason in full:
            detail = f"{detail} [{reason}]"
    tag = "PASS" if ok else "FAIL"
    want = "accept" if expect == "accept" else f"refuse ({reason or 'any'})"
    print(f"  [{tag}] {name}: {verdict} {detail}   (want {want})", flush=True)
    RESULTS.append((name, ok))
    return out


def check(name: str, condition: bool, detail: str = "") -> None:
    tag = "PASS" if condition else "FAIL"
    print(f"  [{tag}] {name} {detail}", flush=True)
    RESULTS.append((name, condition))


def finish(title: str) -> None:
    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{title}: {len(RESULTS) - len(bad)}/{len(RESULTS)} as expected" + (f"; UNEXPECTED: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)


def src(*holders):
    return FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, ph, n) for ph, n in holders]})


def world(asset=bytes(32), **kw):
    """A fresh distributor at the testnet config (dispute 8, rotate 24) with
    one standard campaign of 1,000,000 (budget 970,000), not yet created."""
    w = SimWorld(reward_asset=asset, **kw)
    w.advance_to(w.h + 3)
    window = w.cfg.min_claim_window  # 4 * (dispute_blocks + HEIGHT_WINDOW)
    camp = w.campaign("creator", 1_000_000, w.h + LEAD + 2, w.h + LEAD + 42, Kind.LP_HOLD, POOL, claim_window=window)
    return w, camp


def settle(w):
    """Past the dispute window of a root posted at the current height."""
    w.advance_to(w.h + HEIGHT_WINDOW + w.cfg.dispute_blocks)


def final_root(w, camp, source, end=None):
    """Create nothing; run the engine to `end` (default the campaign end) and
    return the root, with the chain advanced past `end`."""
    end = camp.end if end is None else end
    w.advance_to(end + 1)
    return run_epoch(EngineState(w.launcher_id, height=camp.start), [camp], source, end)[1]


def ephemeral_slot(w, puzzle):
    """A spend of a 0-mojo slot coin at `puzzle`'s hash, as a child of the
    current singleton coin, with the lineage the slot puzzle checks: the coin
    the spend being built would create at that puzzle hash, spent in the same
    bundle. This is the shape of the review's FLPCR-02 attack."""
    inner_ph = w.d.inner_puzzle(w.d.state).tree_hash()
    lineage = to_program(w.clvm, ((w.d.coin.parent_coin_info, (inner_ph, 1)), inner_ph))
    coin = Coin(w.d.coin.coin_id(), puzzle.tree_hash(), 0)
    return CoinSpend(coin, puzzle.serialize(), lineage.serialize())
