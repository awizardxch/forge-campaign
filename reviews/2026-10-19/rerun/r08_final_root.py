"""FLPCR-08 rerun: the minimum claim window leaves no time for the final
root; one veto hands it to the creator (old probe D/d7_final.py).

The fix (create.rue): claim_window >= 3 * dispute_blocks + HEIGHT_WINDOW
(testnet 56, production 146), so the previous root's window, one veto and
repost, and the final root's window fit before reclaim opens. Covered by
tests/test_chain.py::TestStartAndWindow::test_claim_window_floor (the floor
itself) and, as operator policy, scripts/deploy_testnet.py reclaim_blockers()
(tests/test_reclaim_guard.py). No chain test replays the timeline.

The timeline is replayed here twice, at the floor, the creator choosing the
minimum window:
  * posts naming h = max(root end, P - 31), the earliest activation any
    operator can get (post_root also requires h >= the root's end, so a root
    is never active before end + HEIGHT_WINDOW + dispute_blocks);
  * posts naming h = P, which is what the shipped operator tools do
    (deploy_testnet.py cmd_post and service/core.py both pass the node's
    peak): each root activates HEIGHT_WINDOW + dispute_blocks after inclusion,
    so three windows take 3 * (D + 32), more than the floor's 3D + 32.
Previous root r1 covers [.., end - 1] and is posted at end - 1; r2 covers end
and is posted as soon as r1 activates; the guardian vetoes r2 once and the
operator reposts at once. The attack: the creator reclaims the moment the
window opens, while r2 is still pending."""

from flpcr import EngineState, run_epoch

from common import ASSETS, HEIGHT_WINDOW, attempt, check, finish, src, world


def timeline(asset, D, lag, veto):
    """veto: None, "early" (the block after r2 lands) or "late" (the last
    block of r2's window). Returns (world, campaign, r2 activation, reclaim opening)."""
    w, c = world(asset, dispute_blocks=D, rotate_delay=max(24, D + 1))
    window = 3 * D + HEIGHT_WINDOW
    assert c.claim_window == window
    w.create(c, "creator")
    s = src((w.keys.ph("alice"), 3), (w.keys.ph("bob"), 1))
    w.advance_to(c.end)
    st, r1 = run_epoch(EngineState(w.launcher_id, height=c.start), [c], s, c.end - 1)
    w.post(r1, h=max(r1.end, w.h - lag))
    r1_active = w.state().pending.activates_at
    w.advance_to(r1_active)
    st, r2 = run_epoch(st, [c], s, c.end)
    # r2's h must promote r1 (h >= r1's activates_at), so it cannot trail here.
    w.post(r2, h=max(r2.end, r1_active, w.h - lag))
    if veto:
        if veto == "late":
            w.advance_to(w.state().pending.activates_at - 1)
        w.veto(r2.root_id)
        w.post(r2, h=max(r2.end, w.h - lag))
    return w, c, w.state().pending.activates_at, c.end + window


def run(tag, asset):
    print(f"\n{tag}")
    for D in (8, 38):
        for lag, how in ((HEIGHT_WINDOW - 1, "h trails by up to 31"), (0, "h = P (shipped tools)")):
            for veto in (None, "early", "late"):
                w, c, act, opens = timeline(asset, D, lag, veto)
                label = f"D={D}, window {c.claim_window}, {how}, {veto or 'no'} veto: " \
                        f"r2 activates end+{act - c.end}, reclaim opens end+{opens - c.end}"
                if act <= opens:
                    check(label, True, f"-> holders have {opens - act} blocks")
                    continue
                w.advance_to(opens)
                before = w.balance("creator")
                attempt(f"{label} -> ATTACK creator reclaims while r2 is pending", w.reclaim, c.id,
                        expect="refuse")
                if w.balance("creator") > before:
                    print(f"      creator received {w.balance('creator') - before} of budget {c.budget}: "
                          "the holders' accrual in r2 (and any unclaimed r1) is gone")


for tag, asset in ASSETS:
    run(tag, asset)
finish("FLPCR-08")
