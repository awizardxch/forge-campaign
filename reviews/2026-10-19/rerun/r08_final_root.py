"""FLPCR-08 rerun: the minimum claim window leaves no time for the final
root; one veto hands it to the creator (old probe D/d7_final.py).

On 6b8c027 the floor was claim_window >= 3 * dispute_blocks + HEIGHT_WINDOW
(testnet 56, production 146). A root posted at the node's peak (what the
shipped tools do: deploy_testnet.py cmd_post, service/core.py) activates one
cycle, HEIGHT_WINDOW + dispute_blocks, after it lands, and the final root can
only be posted once the root pending across `end` has activated. With one late
veto the final root activates end + 3 * (D + 32): end+120 at D=8, end+210 at
D=38, past the old opening, and the creator reclaimed the holders' accrual.

The fix (fix-bond-reclaim):
  * create.rue: claim_window >= 4 * (dispute_blocks + HEIGHT_WINDOW)
    (testnet 160, production 280): those three cycles plus one more for
    holders to claim the final root before reclaiming opens;
  * reclaim.rue (NEW-3): refused while a dispute is open or while the pending
    root raises the campaign's total, so an operator later than the floor
    assumes still cannot be overtaken by the creator.

The timeline is replayed at the floor, the creator choosing the minimum window:
  * posts naming h = max(root end, P - 31), the earliest activation any
    operator can get (post_root also requires h >= the root's end);
  * posts naming h = P (the shipped tools).
Previous root r1 covers [.., end - 1] and is posted at end - 1; r2 covers end
and is posted as soon as r1 activates; the guardian vetoes r2 once (the block
after it lands, or the last block of its window) and the operator reposts at
once. Each scenario checks holders get at least one full cycle after r2
activates, that alice can claim then, and that the creator's reclaim at the
opening is refused if r2 is still pending (a "slow" operator posting r2 only
the block before the opening)."""

from flpcr import EngineState, run_epoch

from common import ASSETS, HEIGHT_WINDOW, attempt, check, finish, src, world


def timeline(asset, D, lag, veto):
    """veto: None, "early" (the block after r2 lands), "late" (the last block
    of r2's window) or "slow" (no veto; the operator posts r2 only the block
    before reclaiming opens). Returns (world, campaign, r2, r2 activation, reclaim opening)."""
    w, c = world(asset, dispute_blocks=D, rotate_delay=max(24, D + 1))
    window = 4 * (D + HEIGHT_WINDOW)
    assert c.claim_window == window
    w.create(c, "creator")
    s = src((w.keys.ph("alice"), 3), (w.keys.ph("bob"), 1))
    w.advance_to(c.end)
    st, r1 = run_epoch(EngineState(w.launcher_id, height=c.start), [c], s, c.end - 1)
    w.post(r1, h=max(r1.end, w.h - lag))
    r1_active = w.state().pending.activates_at
    w.advance_to(r1_active)
    st, r2 = run_epoch(st, [c], s, c.end)
    if veto == "slow":
        w.advance_to(c.end + window - 1)
    # r2's h must promote r1 (h >= r1's activates_at), so it cannot trail here.
    w.post(r2, h=max(r2.end, r1_active, w.h - lag))
    if veto in ("early", "late"):
        if veto == "late":
            w.advance_to(w.state().pending.activates_at - 1)
        w.veto(r2.root_id)
        w.post(r2, h=max(r2.end, w.h - lag))
    return w, c, r2, w.state().pending.activates_at, c.end + window


def run(tag, asset):
    print(f"\n{tag}")
    for D in (8, 38):
        for lag, how in ((HEIGHT_WINDOW - 1, "h trails by up to 31"), (0, "h = P (shipped tools)")):
            for veto in (None, "early", "late", "slow"):
                w, c, r2, act, opens = timeline(asset, D, lag, veto)
                label = f"D={D}, window {c.claim_window}, {how}, {veto or 'no'} veto: " \
                        f"r2 activates end+{act - c.end}, reclaim opens end+{opens - c.end}"
                if veto == "slow":
                    w.advance_to(opens)
                    attempt(f"{label} -> ATTACK creator reclaims while r2 is pending", w.reclaim, c.id,
                            expect="refuse", reason="clvm raise")  # reclaim.rue: the pending root raises the total
                    w.advance_to(act)
                    attempt(f"{label} -> alice claims r2 once it activates", w.claim, r2, c.id, "alice")
                    continue
                check(label, opens - act >= D + HEIGHT_WINDOW,
                      f"-> holders have {opens - act} blocks (at least one cycle, {D + HEIGHT_WINDOW})")
                w.advance_to(act)
                attempt(f"{label} -> alice claims r2 once it activates", w.claim, r2, c.id, "alice")
                w.advance_to(opens)
                attempt(f"{label} -> the creator reclaims at the opening", w.reclaim, c.id)


for tag, asset in ASSETS:
    run(tag, asset)
finish("FLPCR-08")
