"""FLPCR-13 rerun: batched spends the docs promised are refused by consensus
(old probes C/probe_c3.py, C/probe_c.py P3, F/probes2.py, F/probes3.py).

Fix (no puzzle change): Distributor.prepare raises BundleCollision before any
node sees the bundle (equal reserve outputs, a slot touched twice), and
Distributor.sequence chains generations in one bundle for real collisions.
Covered by tests/test_chain.py::TestBatches and TestSequencer.

For each combination the script shows (1) the driver's BundleCollision, (2)
with the driver's check switched off, the consensus refusal the check
anticipates (so the driver refuses for the real reason), and (3) the split
or sequenced form accepted."""

from flpcr import Kind

from common import ASSETS, LEAD, POOL, BundleCollision, ByDelegate, attempt, check, finish, settle, src, world
from flpcr import EngineState, run_epoch


def unchecked(w, calls, **kw):
    """Push with the driver's collision check off: what a node would say."""
    saved = w.d._check_collisions
    w.d._check_collisions = lambda *a: None
    try:
        return w.push(calls, **kw)
    finally:
        w.d._check_collisions = saved


def setup(asset):
    w, c1 = world(asset)
    alice, bob = w.keys.ph("alice"), w.keys.ph("bob")
    s = src((alice, 1), (bob, 1))
    w.create(c1, "creator")
    c2 = w.campaign("creator", 1_000_000, c1.start, c1.end, Kind.LP_HOLD, POOL, claim_window=c1.claim_window, nonce=1)
    w.create(c2, "creator")
    w.advance_to(c1.start + 21)
    st, r1 = run_epoch(EngineState(w.launcher_id, height=c1.start), [c1, c2], s, c1.start + 20)
    w.post(r1)
    settle(w)
    w.advance_to(c1.end + 1)
    _, r2 = run_epoch(st, [c1, c2], s, c1.end)
    return w, c1, c2, r1, r2, alice, bob


def run(tag, asset):
    print(f"\n{tag}")
    w, c1, c2, r1, r2, alice, bob = setup(asset)

    calls = lambda: [w.claim_call(r1, c1.id, alice), w.claim_call(r1, c1.id, bob)]  # noqa: E731
    attempt("two first claims sharing a neighbour: driver", w.d.prepare, calls(), expect="refuse", reason="slot")
    attempt("  ... driver check off: consensus", unchecked, w, calls(), expect="refuse", reason="DoubleSpend")
    attempt("  ... sequenced in one bundle (Distributor.sequence)", lambda: w.push_batch(w.d.sequence(w.h, [
        __import__("flpcr.chain.driver", fromlist=["ClaimRequest"]).ClaimRequest(c1.id, ph, r1.claim_for(c1.id, ph).cumulative,
                                                                                 r1.claim_for(c1.id, ph).proof)
        for ph in (alice, bob)])))

    w.claim(r1, c2.id, "alice")
    w.post(r2)
    settle(w)
    calls = lambda: [w.claim_call(r2, c1.id, alice), w.claim_call(r2, c2.id, alice)]  # noqa: E731
    inc1 = r2.claim_for(c1.id, alice).cumulative - r1.claim_for(c1.id, alice).cumulative
    inc2 = r2.claim_for(c2.id, alice).cumulative - r1.claim_for(c2.id, alice).cumulative
    check(f"setup: equal increments {inc1} and {inc2}", inc1 == inc2)
    attempt("one holder, two campaigns, equal payouts: driver", w.d.prepare, calls(), expect="refuse",
            reason="payments")
    attempt("  ... driver check off: consensus", unchecked, w, calls(), expect="refuse", reason="DuplicateOutput")
    attempt("  ... split across spends: first", w.claim, r2, c1.id, "alice")
    attempt("  ... split across spends: second", w.claim, r2, c2.id, "alice")

    w.set_delegate("bob", w.keys.ph("hot"))
    calls = lambda: [w.claim_call(r2, cid, bob, auth=ByDelegate(w.keys.ph("hot"))) for cid in (c1.id, c2.id)]  # noqa
    # Bob's two first claims may also share a claim-slot neighbour; the driver names the first clash.
    attempt("a delegate claiming two campaigns for one holder: driver", w.d.prepare, calls(), expect="refuse",
            reason="BundleCollision")
    attempt("  ... driver check off: consensus", unchecked, w, calls(), expect="refuse")

    calls = lambda: [w.d.delegate(w.h, bob, w.keys.ph("hot2"), 1),  # noqa: E731
                     w.claim_call(r2, c1.id, bob, auth=ByDelegate(w.keys.ph("hot")))]
    attempt("a grant plus a delegated claim (different delegates, adjacent slots): driver", w.d.prepare, calls(),
            expect="refuse")

    w2, c = world(asset)
    s = w2.h + LEAD + 2
    p = [10000, 0, 0, None, None, 1]
    mk = lambda n: w2.d.create(w2.h, w2.keys.ph("creator"), 1000, s, s + 40, 1, POOL, p, n, 56)  # noqa: E731
    attempt("two creates with the same amount (identical dev-fee outputs): driver", w2.d.prepare, [mk(0), mk(1)],
            expect="refuse", reason="payments")


for tag, asset in ASSETS:
    run(tag, asset)
finish("FLPCR-13")
