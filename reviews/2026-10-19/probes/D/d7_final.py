from common import *
# Honest operator cadence, minimum claim window 2D, previous root pending across `end`.
w, c, src = setup(8, window=16, length=40)
w.advance_to(c.end - 1 + 1)
st, r1 = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.end - 1)
post_h(w, r1, w.h); P1 = w.h - 1
print("r1 [.., end-1] posted at", P1, "(end =", c.end, ") activates", P1 + 8)
w.advance_to(P1 + 8)
st, r2 = run_epoch(st, [c], src, w.h)
post_h(w, r2, w.h); P2 = w.h - 1
print("r2 covering end posted at", P2, "activates", P2 + 8, "; reclaim opens at", c.end + 16)
w.advance_to(P2 + 8)
print("claimable blocks before reclaim opens:", c.end + 16 - (P2 + 8))
print("bob (never claimed) at", w.h, ":", outcome(w.claim, r2, c.id, "bob"))
# same timeline but the guardian vetoes r2 once and operator reposts
w, c, src = setup(8, window=16, length=40)
w.advance_to(c.end)
st, r1 = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.end - 1)
post_h(w, r1, w.h); w.advance_to(w.h + 8)
st2, r2 = run_epoch(st, [c], src, w.h)
post_h(w, r2, w.h); veto_h(w, 2, w.h)
post_h(w, r2, w.h); act = w.h - 1 + 8
print("after one veto r2 re-posted, activates", act, "reclaim opens", c.end + 16)
w.advance_to(c.end + 16)
print("creator reclaims before r2 activates:", outcome(w.reclaim, c.id), "(budget 970000: holders got 0 of r1+r2 since none claimed)")
