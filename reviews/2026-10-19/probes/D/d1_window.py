from common import *
for D in (38, 8):
    for label, skew in (("honest h=P", 0), ("attack h=P-31", 31)):
        w, c, src = setup(D)
        w.advance_to(c.start + 20 + 31)
        _, r1 = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.start + 20)
        P = w.h
        print(f"D={D} {label}: post at height {P}:", outcome(post_h, w, r1, P - skew))
        # earliest block at which a claim lands (each push mines a block)
        target = P - skew + D
        tries = []
        while True:
            hgt = w.h
            res = outcome(w.claim, r1, c.id, "alice")
            if res.startswith("ACCEPTED"):
                print(f"   first successful claim: chain height {hgt} = P+{hgt-P}  ({res})")
                break
            w.sim.create_block()
            if w.h > P + 60: print("   never"); break
# guardian's effective window in the attack case D=38
w, c, src = setup(38)
w.advance_to(c.start + 20 + 31)
_, r1 = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.start + 20)
P = w.h; post_h(w, r1, P - 31)
w.advance_to(P + 7)
print("D=38 attack: guardian veto at height", w.h, "(P+%d)" % (w.h-P), outcome(veto_h, w, 1, w.h - 31))
# late veto: honest post, nobody spends after activates_at, guardian vetoes with understated h
w, c, src = setup(38)
w.advance_to(c.start + 20 + 31)
_, r1 = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.start + 20)
P = w.h; post_h(w, r1, P)
w.advance_to(P + 38 + 25)
print("late veto at P+%d (activates_at=P+38), h=P+37:" % (w.h-P), outcome(veto_h, w, 1, P + 37))
w2, c2, src2 = setup(38)
w2.advance_to(c2.start + 20 + 31)
_, r2 = run_epoch(EngineState(w2.launcher_id, height=c2.start), [c2], src2, c2.start + 20)
P2 = w2.h; post_h(w2, r2, P2)
w2.advance_to(P2 + 38 + 25)
print("control late veto with h=current:", outcome(veto_h, w2, 1, w2.h))
