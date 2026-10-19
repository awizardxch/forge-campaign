"""Divergence probes for review F. Run with PYTHONPATH=<repo>."""
import sys, traceback
from flpcr.chain.sim import SimWorld
from flpcr.chain.driver import Action, AUTH_MESSAGE, AUTH_DELEGATE, AUTH_SIGNATURE, Distributor
from flpcr.clvm import sha256tree
from flpcr import EngineState, FixtureSource, HolderEvent, Kind, run_epoch, Config, Distributor as Model, Refused

POOL, LP = b"\xb0"*32, b"\x1b"*32
def setup(dispute=8, asset=bytes(32), extra_holders=()):
    w = SimWorld(dispute_blocks=dispute, min_campaign_length=16 if dispute == 8 else 77, reward_asset=asset)
    w.advance_to(w.h + 3)
    return w

def outcome(fn, *a, **k):
    try:
        r = fn(*a, **k); return f"ACCEPTED -> {r}"
    except Exception as e:
        return f"REFUSED: {type(e).__name__}: {str(e)[:160]}"

def probe_double_grant():
    print("== P2 double grant, single revoke ==")
    w = setup()
    cold, hot = w.sim.bls(1), w.sim.bls(1)
    c = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16)
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, cold.puzzle_hash, 1)]})
    w.create(c, "creator")
    w.set_delegate(cold, hot.puzzle_hash)
    first = dict(w.d.delegations)
    print("second grant:", outcome(w.set_delegate, cold, hot.puzzle_hash))
    w.set_delegate(cold, hot.puzzle_hash, grant=0)
    print("driver delegations after revoke:", w.d.delegations)
    w.advance_to(c.end+1)
    _, r = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.end)
    w.post(r); w.advance_to(w.h + 8)
    # the revoked delegate rebuilds the bundle against the first, still-unspent slot
    w.d.delegations.update(first)
    print("revoked delegate claims:", outcome(w.claim_delegated, r, c.id, cold.puzzle_hash, hot))
    m = Model(Config.testnet(w.launcher_id, bytes(32), w.cfg.dev_fee_ph, w.cfg.guardian_ph, w.cfg.router_ph), w.cfg.engine_ph)
    m.set_delegate(1, cold.puzzle_hash, hot.puzzle_hash, True, cold.puzzle_hash)
    m.set_delegate(2, cold.puzzle_hash, hot.puzzle_hash, True, cold.puzzle_hash)
    m.set_delegate(3, cold.puzzle_hash, hot.puzzle_hash, False, cold.puzzle_hash)
    print("model still delegated?", (cold.puzzle_hash, hot.puzzle_hash) in m.delegations)

def lagged_post(w, root, h):
    args = [root.root_id, root.start, root.end, root.root, sorted(root.totals.items()), bytes(32)]
    return w.push([Action("post_root", [h, *args])], [lambda: w.message_from("engine", b"p" + sha256tree(args))])

def probe_dispute_lag(dispute):
    print(f"== P3 dispute window with lagged h, dispute_blocks={dispute} ==")
    w = setup(dispute)
    length = 40 if dispute == 8 else 80
    c = w.campaign("creator", 1_000_000, w.h+2, w.h+2+length, Kind.LP_HOLD, POOL, claim_window=2*dispute)
    alice = w.keys.ph("alice")
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, alice, 1)]})
    w.create(c, "creator")
    w.advance_to(c.end + 40)
    _, r = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.end)
    H = w.h
    # lowest h the chain accepts
    for lag in (32, 31):
        res = outcome(lagged_post, w, r, H - lag)
        print(f"post at sim height {H} naming h={H-lag}:", res)
        if res.startswith("ACC"): break
    pend = w.d.read_state()["pending"]
    print("activates_at", pend["activates_at"], "posted at height", H, "-> intended", H + dispute)
    target = pend["activates_at"]
    w.advance_to(max(target, w.h))
    action, upd = w.claim_action(r, c.id, alice)
    action.solution[0] = target
    auth = w.holder_message("alice", c.id, alice, action.solution[3])
    res = outcome(w.push, [action], [auth], slot_updates=upd)
    print(f"claim at sim height {w.h} (blocks after post: {w.h - H}) naming h={target}:", res)

def probe_retro_create():
    print("== P6 create with start in the past ==")
    w = setup(); w.advance_to(60)
    H = w.h
    res = outcome(w.create_raw, "creator", 1000, H-31, H+20, 1, POOL, [10000,0,0,None,None,1,None,None,25000], 0, 16, h=H-31)
    print(f"sim height {H}, campaign start {H-31}:", res)

def probe_two_delegated_claims():
    print("== P4 hot wallet claims two campaigns for one cold wallet in one spend ==")
    w = setup()
    cold, hot = w.sim.bls(1), w.sim.bls(1)
    c1 = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16)
    c2 = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16, nonce=1)
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
    print("coin spends:", len(spends), "distinct coins:", len(set(ids)))
    print("push:", outcome(w.push, [a1, a2], secret_keys=[hot.sk], slot_updates={**u1, **u2},
                           delegation_updates={(cold.puzzle_hash, hot.puzzle_hash): True}))
    # Same thing with the holder signing directly: fine (different claim keys)
    b1, v1 = w.claim_action(r, c1.id, cold.puzzle_hash, auth=AUTH_SIGNATURE, auth_data=cold.pk.to_bytes())
    b2, v2 = w.claim_action(r, c2.id, cold.puzzle_hash, auth=AUTH_SIGNATURE, auth_data=cold.pk.to_bytes())
    print("holder signs both:", outcome(w.push, [b1, b2], secret_keys=[cold.sk], slot_updates={**v1, **v2}))

def probe_multi():
    print("== P5 multi-action spends ==")
    w = setup()
    hs = [f"m{i}" for i in range(12)]
    c = w.campaign("creator", 1_000_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16)
    c2 = w.campaign("creator2", 500_000, w.h+2, w.h+42, Kind.LP_HOLD, POOL, claim_window=16)
    # create + create in one spend
    def create_action(camp, creator):
        sol = [w.h, w.keys.ph(creator), camp.amount, camp.start, camp.end, 1, camp.target, camp.params.as_clvm(), camp.nonce, camp.claim_window]
        def b():
            coin = w.sim.new_coin(w.keys.ph(creator), camp.amount)
            return [w.keys.spend(coin, creator, [])] + w.message_from("router", b"c" + camp.id)
        return Action("create", sol), b
    (a1, b1), (a2, b2) = create_action(c, "creator"), create_action(c2, "creator2")
    print("create+create:", outcome(w.push, [a1, a2], [b1, b2]), "dev balance", w.balance("dev"))
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, w.keys.ph(x), 1) for x in hs]})
    w.advance_to(c.start + 21)
    st, r1 = run_epoch(EngineState(w.launcher_id, height=c.start), [c, c2], src, c.start + 20)
    w.post(r1); w.advance_to(w.h + 8)
    # two non-adjacent new holders in one spend
    keys = sorted((Distributor.claim_key(c.id, w.keys.ph(x)), x) for x in hs)
    pick = [keys[0][1], keys[5][1]]
    acts, builders, upd = [], [], {}
    for x in pick:
        a, u = w.claim_action(r1, c.id, w.keys.ph(x)); acts.append(a); upd.update(u)
        builders.append(w.holder_message(x, c.id, w.keys.ph(x), a.solution[3]))
    print("two non-adjacent inserts:", outcome(w.push, acts, builders, slot_updates=upd))
    # delegate grant + delegated claim in one spend
    cold, hot = w.sim.bls(1), w.sim.bls(1)
    print("(skipped grant+claim: cold not a holder)")
    # post_root + claim in one spend
    w.advance_to(c.end + 1)
    _, r2 = run_epoch(st, [c, c2], src, c.end)
    a, u = w.claim_action(r1, c2.id, w.keys.ph(keys[2][1]))
    args = [r2.root_id, r2.start, r2.end, r2.root, sorted(r2.totals.items()), bytes(32)]
    post = Action("post_root", [w.h, *args])
    print("post_root + claim:", outcome(w.push, [post, a], [lambda: w.message_from("engine", b"p"+sha256tree(args)),
          w.holder_message(keys[2][1], c2.id, w.keys.ph(keys[2][1]), a.solution[3])], slot_updates=u))

if __name__ == "__main__":
    for p in sys.argv[1:] or ["2", "3a", "3b", "6", "4", "5"]:
        try:
            {"2": probe_double_grant, "3a": lambda: probe_dispute_lag(8), "3b": lambda: probe_dispute_lag(38),
             "6": probe_retro_create, "4": probe_two_delegated_claims, "5": probe_multi}[p]()
        except Exception:
            traceback.print_exc()
