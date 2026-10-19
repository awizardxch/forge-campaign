"""Reviewer C probes: value conservation, reserve, CAT ring, arithmetic.
Run: PYTHONPATH=<repo> python probe_c.py [xch|cat]"""
import sys, traceback
import sys; sys.path.insert(0, __file__.rsplit("/", 1)[0])
from probe_lib import *
from probe_lib import Action, Distributor, AUTH_MESSAGE, CatSpend, Spend
print("=== asset", "XCH" if ASSET == bytes(32) else "CAT", "===")

# ---- 1. campaign-table exhaustion with dust, far-future campaigns -----------------
print("\n-- P1 max_campaigns exhaustion (amount=1, fee=0, start far future)")
w = world()
far = 10**12
for i in range(32):
    raw(w, creator="griefer", amount=1, start=w.h + far, end=w.h + far + 16, nonce=i)
st = w.d.read_state()
print(f"    campaigns={len(st['campaigns'])} reserve={st['reserve']} dev_fee_paid={w.balance('dev')}")
outcome("honest create after 32 dust campaigns", setup_campaign, w)
c0 = list(st["campaigns"])[0]
outcome("griefer reclaim of dust (not before end+window)", w.reclaim, c0)
# control: in a fresh world the same honest create works
w2 = world(); outcome("control honest create (fresh world)", setup_campaign, w2)

# ---- 2. create arithmetic ------------------------------------------------------------
print("\n-- P2 create arithmetic")
w = world()
outcome("amount=0", raw, w, amount=0)
outcome("amount=-1000 deposit 0", raw, w, amount=-1000, deposit=0)
outcome("amount=1 (fee floor 0, budget 1)", raw, w, amount=1, nonce=1)
outcome("amount=33 (fee 0)", raw, w, amount=33, nonce=2)
print(f"    dev balance after amount 1 and 33: {w.balance('dev')}")
outcome("amount=34 (fee 1)", raw, w, amount=34, nonce=3)
print(f"    dev balance: {w.balance('dev')}")
invariant(w)
outcome("amount=2**64 (reserve overflow)", raw, w, amount=2**64, nonce=4)
outcome("amount=2**64-1 deposit short? (huge)", raw, w, amount=2**63, nonce=5)
outcome("start=h-1 (past)", raw, w, start=w.h - 1, end=w.h + 40, nonce=6)
outcome("end<start", raw, w, start=w.h + 40, end=w.h + 2, nonce=7)
outcome("claim_window negative", raw, w, claim_window=-5, nonce=8)
outcome("deposit=amount-1", raw, w, amount=5000, deposit=4999, nonce=9)
invariant(w)

# ---- 3. two creates, same amount, one spend -> duplicate dev-fee payments ---------------
print("\n-- P3 duplicate outputs")
w = world()
from flpcr.clvm import sha256tree

def create_actions(w, amounts, creator="creator"):
    acts, msgs, total = [], [], 0
    for i, amt in enumerate(amounts):
        cph = w.keys.ph(creator)
        st, en = w.h + 2, w.h + 40
        sol = [w.h, cph, amt, st, en, 1, POOL, list(PARAMS), 100 + i, WINDOW]
        cid = sha256tree(["flpcr-campaign-v1", w.launcher_id, cph, w.cfg.reward_asset, amt, st, en, 1, POOL,
                          sha256tree(list(PARAMS)), 100 + i, w.cfg.fee_bps, WINDOW])
        acts.append(Action("create", sol))
        msgs.append((lambda cid=cid: w.message_from("router", b"c" + cid)))
        total += amt
    return acts, msgs, total

def push_creates(w, amounts):
    acts, msgs, total = create_actions(w, amounts)
    if w.d.is_cat:
        cat = w.mint_cat("creator", total)
        return w.push(acts, msgs, cat_spends=[CatSpend(cat, Spend(w.keys.puzzle("creator"), w.clvm.nil()))])
    def dep():
        coin = w.sim.new_coin(w.keys.ph("creator"), total)
        return [w.keys.spend(coin, "creator", [])]
    return w.push(acts, msgs + [dep])

outcome("control: two creates, different fees (1000, 2000)", push_creates, w, [1000, 2000])
invariant(w)
outcome("two creates same amount (1000,1000) one spend", push_creates, w, [1000, 1000])
invariant(w)

# ---- 4. claims, multi-action ----------------------------------------------------------
print("\n-- P4 claims and multi-action")
w = world()
holders = (("alice", 3), ("bob", 1))
c = setup_campaign(w)
src = src_for(w, holders)
r = make_root(w, [c], src, c.end)
w.post(r); w.advance_to(w.h + w.cfg.dispute_blocks)
alice, bob = w.keys.ph("alice"), w.keys.ph("bob")
cl = r.claim_for(c.id, alice)
print(f"    alice cumulative={cl.cumulative} total={r.totals[c.id]} budget={c.budget}")
outcome("claim cumulative=0 (amount 0)", w.claim, r, c.id, "alice", cumulative=0)
outcome("claim cumulative=-5", w.claim, r, c.id, "alice", cumulative=-5)
outcome("claim cumulative != leaf (inflated)", w.claim, r, c.id, "alice", cumulative=cl.cumulative + 1)
outcome("honest alice claim", w.claim, r, c.id, "alice")
invariant(w)
outcome("alice reclaim-again (cumulative unchanged)", w.claim, r, c.id, "alice")

# same holder, two claims in one spend (second on the ephemeral slot)
a1, u1 = w.claim_action(r, c.id, bob)
auth1 = w.holder_message("bob", c.id, bob, a1.solution[3])
key = Distributor.claim_key(c.id, bob)
# second action: update the slot created by the first, same cumulative -> amount 0
_, lk_rk = None, None
a2 = Action("claim", [w.h, c.id, bob, a1.solution[3], r.claim_for(c.id, bob).proof, 0,
                      (a1.solution[3], (u1[key][1], u1[key][2])), AUTH_MESSAGE, None])
outcome("bob claim twice in one spend (same cumulative)", w.push, [a1, a2], [auth1, auth1], slot_updates=u1)
invariant(w)

# claim + reclaim same campaign in one spend (past window)
w.advance_to(c.end + WINDOW)
a1, u1 = w.claim_action(r, c.id, bob)
auth1 = w.holder_message("bob", c.id, bob, a1.solution[3])
rec = Action("reclaim", [w.h, c.id, AUTH_MESSAGE, None])
recmsg = lambda: w.message_from("creator", b"k" + w.reclaim_message(c.id))
before_b, before_c = w.balance("bob"), w.balance("creator")
ok = outcome("claim(bob)+reclaim same spend", w.push, [a1, rec], [auth1, recmsg], slot_updates=u1)
print(f"    bob +{w.balance('bob') - before_b}, creator +{w.balance('creator') - before_c}")
invariant(w)

# reclaim then claim
w = world()
c = setup_campaign(w)
r = make_root(w, [c], src_for(w, holders), c.end)
w.post(r); w.advance_to(c.end + WINDOW)
a1, u1 = w.claim_action(r, c.id, w.keys.ph("bob"))
auth1 = w.holder_message("bob", c.id, w.keys.ph("bob"), a1.solution[3])
rec = Action("reclaim", [w.h, c.id, AUTH_MESSAGE, None])
recmsg = lambda: w.message_from("creator", b"k" + w.reclaim_message(c.id))
outcome("reclaim then claim same spend", w.push, [rec, a1], [recmsg, auth1], slot_updates=u1)
invariant(w)

# ---- 5. cross-campaign: leaves of A over A's total cannot reach B ---------------------
print("\n-- P5 cross-campaign drain")
w = world()
cA = setup_campaign(w, nonce=1)
cB = setup_campaign(w, nonce=2)
src = src_for(w, holders)
r = make_root(w, [cA, cB], src, cA.end)
# operator posts A's total low (1) but leaves are honest: A holders try to take more
tot = dict(r.totals); tot[cA.id] = 1
w.post(r, totals=tot); w.advance_to(w.h + w.cfg.dispute_blocks)
outcome("A alice claim with A total=1 (leaf 727500)", w.claim, r, cA.id, "alice")
outcome("A alice claim cumulative=1 against bogus proof", w.claim, r, cA.id, "alice", cumulative=1)
outcome("B alice honest claim", w.claim, r, cB.id, "alice")
invariant(w)

# ---- 6. post_root arithmetic -----------------------------------------------------------
print("\n-- P6 post_root totals")
w = world()
c = setup_campaign(w)
r = make_root(w, [c], src_for(w, holders), c.start + 20)
outcome("negative total first root", w.post, r, totals={c.id: -1})
outcome("total = emittable+1", w.post, r, totals={c.id: c.budget * 20 // 40 + 1})
outcome("end > h (future)", w.post, r, end=w.h + 5)
outcome("unknown campaign id", w.post, r, totals={b"\x01" * 32: 0, c.id: r.totals[c.id]})
outcome("honest post", w.post, r)
invariant(w)
