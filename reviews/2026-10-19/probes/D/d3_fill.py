from common import *
w = SimWorld(dispute_blocks=38, min_campaign_length=77)
w.advance_to(w.h + 3)
YEAR = 365 * 4608
ids = []
for i in range(32):
    c = w.campaign("griefer", 1, w.h + 1, w.h + 1 + YEAR, Kind.LP_HOLD, POOL, claim_window=YEAR, nonce=i)
    w.create(c, "griefer"); ids.append(c.id)
st = w.d.read_state()
print("campaigns", len(st["campaigns"]), "reserve", st["reserve"], "griefer deposit total mojos", 32)
honest = w.campaign("creator", 10**12, w.h + 1, w.h + 1 + 1000, Kind.LP_HOLD, POOL, claim_window=76, nonce=99)
print("33rd honest campaign:", outcome(w.create, honest, "creator"))
w.advance_to(w.h + 77 + 76 + 5)
print("stranger reclaim of a griefer campaign (after a short window would have passed):", outcome(w.reclaim, ids[0], sender="creator"))
print("unlock height for griefer slots = start+YEAR+YEAR ~", 2*YEAR, "blocks (~730 days), and only if griefer reclaims")
