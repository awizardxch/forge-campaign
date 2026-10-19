from common import *
w, A, src = setup(8, length=200)
w.advance_to(A.start + 40)
st, r1 = run_epoch(EngineState(w.launcher_id, height=A.start), [A], src, A.start + 40)
post_h(w, r1, w.h); w.advance_to(w.h + 8)
H = w.h
# B starts 31 blocks before the height its create names (retroactive), i.e. before r1.end
def mk(start):
    return w.campaign("creator2", 1_000_000, start, start + 77, Kind.LP_HOLD, POOL, claim_window=16, nonce=start)
B = mk(H - 31)
print("r1.end", r1.end, "H", H, "B.start", B.start)
print("create B start=H-31 with h=H-31:", outcome(lambda: w.create_raw("creator2", B.amount, B.start, B.end, int(B.kind), B.target, B.params.as_clvm(), B.nonce, B.claim_window, h=H-31)))
B2 = mk(H - 32)
print("control start=H-32 h=H-32:", outcome(lambda: w.create_raw("creator2", B2.amount, B2.start, B2.end, int(B2.kind), B2.target, B2.params.as_clvm(), B2.nonce, B2.claim_window, h=H-32)))
print("control start=H-32 h=H-31:", outcome(lambda: w.create_raw("creator2", B2.amount, B2.start, B2.end, int(B2.kind), B2.target, B2.params.as_clvm(), B2.nonce, B2.claim_window, h=H-31)))
w.advance_to(B.end + 1)
st2, r2 = run_epoch(st, [A, B], src, B.end)
bud = 970_000
print("engine r2 window", r2.start, r2.end, "B total", r2.totals.get(B.id), "B emittable(end)", bud, "lost to holders", bud - r2.totals.get(B.id, 0), "=", (r1.end - B.start), "/77 blocks")
