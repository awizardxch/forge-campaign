import sys
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
from probes import *
w = setup()
c = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16)
w.create(c, "creator")
msg = w.reclaim_message(c.id)
act = Action("reclaim", [c.end + c.claim_window, c.id, AUTH_MESSAGE, None])
print("height", w.h, "reclaim naming h =", c.end + c.claim_window, ":", outcome(w.push, [act], [lambda: w.message_from("creator", b"k" + msg)]))
