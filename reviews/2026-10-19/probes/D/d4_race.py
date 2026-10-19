from common import *
w, c, src = setup(8)
w.advance_to(c.start + 21)
_, r1 = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.start + 20)
totals_list = [(k, v) for k, v in sorted(r1.totals.items())]
args = [r1.root_id, r1.start, r1.end, r1.root, totals_list, bytes(32)]
spends, new_state, _ = w.d.spend([Action("post_root", [w.h, *args])])
spends += w.message_from("engine", b"p" + sha256tree(args))
# a stranger with no stake: a fresh BLS wallet grants itself a delegate (singleton spend, no fee)
s = w.sim.bls(0)
w.set_delegate(s, w.keys.ph("x"), 1)
print("stranger delegate spend landed; operator's prebuilt post_root:", outcome(w.sim.spend_coins, spends, []))
# control: rebuilt against the new coin
print("rebuilt post_root:", outcome(post_h, w, r1, w.h) [:9])
