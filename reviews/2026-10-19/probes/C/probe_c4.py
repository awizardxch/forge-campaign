"""Full table (32 campaigns, some with a 2**63-scale budget): post a root naming all 32, claim; measure cost."""
import sys
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from probe_lib import *
from flpcr.model import Kind

w = world()
S, E = w.h + 100, w.h + 140
camps = [w.campaign("creator", 1_000_000, S, E, Kind.LP_HOLD, POOL, claim_window=WINDOW, nonce=i) for i in range(32)]
for c in camps: w.create(c, "creator")
src = src_for(w, [(f"h{i}", 1 + i) for i in range(20)])
w.advance_to(E + 1)
_, r = run_epoch(EngineState(w.launcher_id, height=S), camps, src, E)
orig_spend = w.d.spend
costs = []
def spy(actions, cat_spends=()):
    spends, ns, pa = orig_spend(actions, cat_spends)
    s = spends[0]
    prog = w.clvm.deserialize(s.puzzle_reveal); sol = w.clvm.deserialize(s.solution)
    out = prog.run(sol, 11_000_000_000, False)
    costs.append((actions[0].leaf, out.cost))
    return spends, ns, pa
w.d.spend = spy
outcome("post root naming 32 campaigns", w.post, r)
w.advance_to(w.h + w.cfg.dispute_blocks)
outcome("claim in campaign 0 (last in list)", w.claim, r, camps[0].id, "h5")
outcome("claim in campaign 31 (first in list)", w.claim, r, camps[31].id, "h5")
w.advance_to(E + WINDOW)
outcome("reclaim campaign 0", w.reclaim, camps[0].id)
invariant(w)
print("    singleton puzzle cost:", costs)
