"""Q2 redo: one holder, two campaigns with identical schedules, batched updates of equal size -> DuplicateOutput."""
import sys
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from probe_lib import *
from flpcr.model import Kind

print("=== Q2b", "XCH" if ASSET == bytes(32) else "CAT", "===")
w = world()
S, E = w.h + 2, w.h + 42
cA = w.campaign("creator", 1_000_000, S, E, Kind.LP_HOLD, POOL, claim_window=WINDOW, nonce=1)
cB = w.campaign("creator", 1_000_000, S, E, Kind.LP_HOLD, POOL, claim_window=WINDOW, nonce=2)
w.create(cA, "creator"); w.create(cB, "creator")
src = src_for(w, (("alice", 3), ("bob", 1)))
w.advance_to(S + 21)
st, r1 = run_epoch(EngineState(w.launcher_id, height=S), [cA, cB], src, S + 20)
w.post(r1); w.advance_to(w.h + w.cfg.dispute_blocks)
w.claim(r1, cA.id, "alice"); w.claim(r1, cB.id, "alice")
w.advance_to(E + 1)
st, r2 = run_epoch(st, [cA, cB], src, E)
w.post(r2); w.advance_to(w.h + w.cfg.dispute_blocks)
alice = w.keys.ph("alice")
aA, uA = w.claim_action(r2, cA.id, alice); aB, uB = w.claim_action(r2, cB.id, alice)
prevA = w.d.slots[Distributor.claim_key(cA.id, alice)].claimed
prevB = w.d.slots[Distributor.claim_key(cB.id, alice)].claimed
print(f"    increments A={aA.solution[3]-prevA} B={aB.solution[3]-prevB}")
mA = w.holder_message("alice", cA.id, alice, aA.solution[3]); mB = w.holder_message("alice", cB.id, alice, aB.solution[3])
ups = dict(uA); ups.update(uB)
outcome("alice batches A+B updates, equal increments", w.push, [aA, aB], [mA, mB], slot_updates=ups)
invariant(w)
outcome("control: A alone", w.claim, r2, cA.id, "alice")
outcome("control: B alone", w.claim, r2, cB.id, "alice")
invariant(w)
