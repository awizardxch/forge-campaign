from common import *
import chia_wallet_sdk as s
from flpcr.chain.driver import MAX_COST
BLOCK = 11_000_000_000
def cost_of(w, spends):
    sing = spends[0]
    p = w.clvm.deserialize(sing.puzzle_reveal); sol = w.clvm.deserialize(sing.solution)
    run = p.run(sol, MAX_COST, False).cost
    return run, s.spend_bundle_cost(spends)
def measure(w, label, actions, extra=()):
    spends, _, _ = w.d.spend(actions)
    for b in extra: spends += b()
    run, total = cost_of(w, spends)
    print(f"{label:58s} singleton CLVM {run:>12,}  bundle {total:>13,}  = {100*total/BLOCK:5.2f}% of block")
    return spends

for n in (1, 32):
    w = SimWorld(dispute_blocks=8)
    w.advance_to(w.h + 3)
    camps = []
    holders = [w.keys.ph(f"h{i}") for i in range(8)]
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, ph, i + 1) for i, ph in enumerate(holders)]})
    for i in range(n):
        c = w.campaign(f"cr{i}", 1_000_000 + i, w.h + 2, w.h + 202, Kind.LP_HOLD, POOL, claim_window=16, nonce=i)
        if i == n - 1:
            sol = [w.h, w.keys.ph(f"cr{i}"), c.amount, c.start, c.end, int(c.kind), c.target, c.params.as_clvm(), c.nonce, c.claim_window]
            measure(w, f"[{n} campaigns] create #{n}", [Action("create", sol)], [lambda c=c: w.message_from("router", b"c" + c.id)])
        w.create(c, f"cr{i}"); camps.append(c)
    w.advance_to(camps[0].start + 40)
    st, r1 = run_epoch(EngineState(w.launcher_id, height=camps[0].start), camps, src, w.h - 1)
    totals_list = [(k, v) for k, v in sorted(r1.totals.items())]
    args = [r1.root_id, r1.start, r1.end, r1.root, totals_list, bytes(32)]
    measure(w, f"[{n} campaigns] post_root #1 ({len(totals_list)} totals)", [Action("post_root", [w.h, *args])], [lambda: w.message_from("engine", b"p" + sha256tree(args))])
    post_h(w, r1, w.h); w.advance_to(w.h + 8)
    w.advance_to(w.h + 20)
    st, r2 = run_epoch(st, camps, src, w.h - 1)
    totals_list = [(k, v) for k, v in sorted(r2.totals.items())]
    args = [r2.root_id, r2.start, r2.end, r2.root, totals_list, bytes(32)]
    measure(w, f"[{n} campaigns] post_root #2 (with previous totals)", [Action("post_root", [w.h, *args])], [lambda: w.message_from("engine", b"p" + sha256tree(args))])
    # claim: worst case is the campaign last in state list (first created)
    c = camps[0]
    a, upd = w.claim_action(r1, c.id, holders[7])
    measure(w, f"[{n} campaigns] one claim (insert, message auth)", [a], [w.holder_message(f"h7", c.id, holders[7], a.solution[3])])
    # several claims in one spend for distinct holders of different campaigns
    acts, builders = [], []
    for i in range(min(n, 8)):
        c = camps[i]; ph = holders[i]
        a, _ = w.claim_action(r1, c.id, ph)
        acts.append(a); builders.append(w.holder_message(f"h{i}", c.id, ph, a.solution[3]))
    # (slot neighbours may conflict for inserts; only measuring CLVM cost of the singleton)
    sp, _, _ = w.d.spend(acts)
    run = w.clvm.deserialize(sp[0].puzzle_reveal).run(w.clvm.deserialize(sp[0].solution), MAX_COST, False).cost
    print(f"{'['+str(n)+' campaigns] '+str(len(acts))+' claims in one spend (singleton only)':58s} singleton CLVM {run:>12,}")
