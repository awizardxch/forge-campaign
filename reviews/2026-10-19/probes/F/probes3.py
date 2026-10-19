import sys
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
from probes import *

print("== equal payouts to one holder from two campaigns, both slots exist ==")
w = setup()
hb = w.sim.bls(1)
c1 = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16)
c2 = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16, nonce=1)
src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, hb.puzzle_hash, 1)]})
w.create(c1, "creator"); w.create(c2, "creator")
w.advance_to(c1.start + 21)
st, r1 = run_epoch(EngineState(w.launcher_id, height=c1.start), [c1, c2], src, c1.start + 20)
w.post(r1); w.advance_to(w.h+8)
print(" first claims separately:", w.claim_signed(r1, c1.id, hb), w.claim_signed(r1, c2.id, hb))
w.advance_to(c1.end+1)
_, r2 = run_epoch(st, [c1, c2], src, c1.end)
w.post(r2); w.advance_to(w.h+8)
b1, v1 = w.claim_action(r2, c1.id, hb.puzzle_hash, auth=AUTH_SIGNATURE, auth_data=hb.pk.to_bytes())
b2, v2 = w.claim_action(r2, c2.id, hb.puzzle_hash, auth=AUTH_SIGNATURE, auth_data=hb.pk.to_bytes())
print(" insert flags", b1.solution[5], b2.solution[5])
print(" claim both in one spend:", outcome(w.push, [b1, b2], secret_keys=[hb.sk], slot_updates={**v1, **v2}))
m = Model(Config.testnet(w.launcher_id, bytes(32), w.cfg.dev_fee_ph, w.cfg.guardian_ph, w.cfg.router_ph), w.cfg.engine_ph)

print("== disjoint-gap inserts + bookkeeping ==")
w = setup()
hs = [f"m{i}" for i in range(10)]
c = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16)
w.create(c, "creator")
src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, w.keys.ph(x), 1+i) for i, x in enumerate(hs)]})
w.advance_to(c.end+1)
_, r = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.end)
w.post(r); w.advance_to(w.h+8)
keys = sorted((Distributor.claim_key(c.id, w.keys.ph(x)), x) for x in hs)
w.claim(r, c.id, keys[3][1]); w.claim(r, c.id, keys[6][1])
acts, bs, upd = [], [], {}
for x in (keys[1][1], keys[8][1]):
    a, u = w.claim_action(r, c.id, w.keys.ph(x)); acts.append(a); upd.update(u)
    bs.append(w.holder_message(x, c.id, w.keys.ph(x), a.solution[3]))
print(" batch:", outcome(w.push, acts, bs, slot_updates=upd))
for i in (0, 2, 4, 5, 7, 9):
    print("  follow-up", i, outcome(w.claim, r, c.id, keys[i][1]))
print(" reserve on chain == state:", w.reserve_on_chain() == w.d.read_state()["reserve"])
