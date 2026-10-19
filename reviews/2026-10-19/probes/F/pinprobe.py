"""Height-pin probe with a properly authorized early reclaim. Run with FLPCR_COMPILED set to compare builds."""
import sys
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
from probes import *
w = setup()
c = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16)
bob = w.keys.ph("bob")
src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, bob, 1)]})
w.create(c, "creator"); w.advance_to(c.end+1)
_, r = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.start + 20)
w.post(r); w.advance_to(w.h+8)
assert w.h < c.end + c.claim_window
a, u = w.claim_action(r, c.id, bob)
auth = w.holder_message("bob", c.id, bob, a.solution[3])
early = Action("reclaim", [w.h + c.claim_window + 100, c.id, AUTH_MESSAGE, None])
creator_msg = lambda: w.message_from("creator", b"k" + w.reclaim_message(c.id))
before = w.balance("creator")
print("early reclaim at", w.h, "window opens", c.end + c.claim_window, ":", outcome(w.push, [a, early], [auth, creator_msg], slot_updates=u))
print("creator received", w.balance("creator") - before)
