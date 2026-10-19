import sys
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
from probes import *

def p_same_amount_creates():
    print("== two creates, same amount, one spend ==")
    w = setup()
    c = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16)
    c2 = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16, nonce=1)
    acts, bs = [], []
    for camp in (c, c2):
        sol = [w.h, w.keys.ph("creator"), camp.amount, camp.start, camp.end, 1, camp.target, camp.params.as_clvm(), camp.nonce, camp.claim_window]
        def b(camp=camp):
            coin = w.sim.new_coin(w.keys.ph("creator"), camp.amount)
            return [w.keys.spend(coin, "creator", [])] + w.message_from("router", b"c" + camp.id)
        acts.append(Action("create", sol)); bs.append(b)
    print(outcome(w.push, acts, bs))
    m = Model(Config.testnet(w.launcher_id, bytes(32), w.cfg.dev_fee_ph, w.cfg.guardian_ph, w.cfg.router_ph), w.cfg.engine_ph)
    print("model:", outcome(lambda: [m.create(w.h, w.cfg.router_ph, x, x.amount) for x in (c, c2)]))

def p_holder_two_campaigns(diff):
    print(f"== one holder, two campaigns, one spend, different amounts={diff} ==")
    w = setup()
    hb = w.sim.bls(1)
    c1 = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16)
    c2 = w.campaign("creator", 2_000_000 if diff else 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16, nonce=1)
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, hb.puzzle_hash, 1)]})
    w.create(c1, "creator"); w.create(c2, "creator")
    w.advance_to(c1.end+1)
    _, r = run_epoch(EngineState(w.launcher_id, height=c1.start), [c1, c2], src, c1.end)
    w.post(r); w.advance_to(w.h+8)
    b1, v1 = w.claim_action(r, c1.id, hb.puzzle_hash, auth=AUTH_SIGNATURE, auth_data=hb.pk.to_bytes())
    b2, v2 = w.claim_action(r, c2.id, hb.puzzle_hash, auth=AUTH_SIGNATURE, auth_data=hb.pk.to_bytes())
    print(" holder signs both:", outcome(w.push, [b1, b2], secret_keys=[hb.sk], slot_updates={**v1, **v2}))
    return w, hb, r, (c1, c2)

def p_delegate_two(diff=True):
    print("== delegate claims two campaigns (different amounts) for one holder in one spend ==")
    w = setup()
    cold, hot = w.sim.bls(1), w.sim.bls(1)
    c1 = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16)
    c2 = w.campaign("creator", 2_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16, nonce=1)
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, cold.puzzle_hash, 1)]})
    w.create(c1, "creator"); w.create(c2, "creator")
    w.set_delegate(cold, hot.puzzle_hash)
    w.advance_to(c1.end+1)
    _, r = run_epoch(EngineState(w.launcher_id, height=c1.start), [c1, c2], src, c1.end)
    w.post(r); w.advance_to(w.h+8)
    data = (hot.puzzle_hash, (AUTH_SIGNATURE, hot.pk.to_bytes()))
    a1, u1 = w.claim_action(r, c1.id, cold.puzzle_hash, auth=AUTH_DELEGATE, auth_data=data)
    a2, u2 = w.claim_action(r, c2.id, cold.puzzle_hash, auth=AUTH_DELEGATE, auth_data=data)
    spends, _, _ = w.d.spend([a1, a2])
    ids = [s.coin.coin_id() for s in spends]
    print(" spends", len(spends), "distinct", len(set(ids)))
    print(" push:", outcome(w.push, [a1, a2], secret_keys=[hot.sk], slot_updates={**u1, **u2},
                           delegation_updates={(cold.puzzle_hash, hot.puzzle_hash): True}))
    # grant + delegated claim in one spend
    w2 = setup(); 
    c = w2.campaign("creator", 1_000_000, w2.h+2, w2.h+42, Kind.LP_HOLD, POOL, claim_window=16)
    w2.create(c, "creator"); w2.advance_to(c.end+1)
    _, r = run_epoch(EngineState(w2.launcher_id, height=c.start), [c], src, c.end)
    w2.post(r); w2.advance_to(w2.h+8)
    g = Action("delegate", [w2.h, cold.puzzle_hash, hot.puzzle_hash, 1, AUTH_SIGNATURE, cold.pk.to_bytes()])
    a, u = w2.claim_action(r, c.id, cold.puzzle_hash, auth=AUTH_DELEGATE, auth_data=data)
    print(" grant + delegated claim:", outcome(w2.push, [g, a], secret_keys=[cold.sk, hot.sk], slot_updates=u,
              delegation_updates={(cold.puzzle_hash, hot.puzzle_hash): True}))

def p_nonadjacent():
    print("== two first claims in different gaps ==")
    w = setup()
    hs = [f"m{i}" for i in range(8)]
    c = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16)
    w.create(c, "creator")
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, w.keys.ph(x), 1) for x in hs]})
    w.advance_to(c.end+1)
    _, r = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.end)
    w.post(r); w.advance_to(w.h+8)
    keys = sorted((Distributor.claim_key(c.id, w.keys.ph(x)), x) for x in hs)
    w.claim(r, c.id, keys[3][1])
    acts, bs, upd = [], [], {}
    for x in (keys[0][1], keys[6][1]):
        a, u = w.claim_action(r, c.id, w.keys.ph(x)); acts.append(a); upd.update(u)
        bs.append(w.holder_message(x, c.id, w.keys.ph(x), a.solution[3]))
    print(" ", outcome(w.push, acts, bs, slot_updates=upd))
    # then all three again? check driver bookkeeping by a follow-up single claim of keys[6] (nothing new) and keys[4]
    print("  follow-up insert in gap 3..6:", outcome(w.claim, r, c.id, keys[4][1]))
    print("  slot list consistent:", sorted(w.d.slots) == sorted([bytes(32), b'\xff'*32] + [keys[i][0] for i in (0,3,4,6)]))

p_same_amount_creates(); p_holder_two_campaigns(False); p_holder_two_campaigns(True); p_delegate_two(); p_nonadjacent()
