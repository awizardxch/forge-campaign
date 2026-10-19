"""Verified-safe battery: each attack is paired with an honest control in the same world."""
from hashlib import sha256
from chia_wallet_sdk import (Cat, CatInfo, CatSpend, Coin, LineageProof, Spend, SpendBundle, Signature, PublicKey,
                             curry_tree_hash)
from common import *

STD = bytes.fromhex("e9aaa49f45bad5c889b86ee3341550c155cfdd10c3a6757de618d20612fffd52")
INF = bytes.fromhex("c0" + "00" * 47)


def std_ph(pk):
    return curry_tree_hash(STD, [sha256(b"\x01" + pk).digest()])


def setup(asset=bytes(32)):
    w, c = world(asset)
    w.create(c, "creator")
    K, K2 = w.sim.bls(1), w.sim.bls(1)
    alice, bob = w.keys.ph("alice"), w.keys.ph("bob")
    inf_ph = std_ph(INF)
    w.advance_to(c.end + 1)
    r = make_root(w, 1, c.start, c.end, [(c.id, alice, 100), (c.id, bob, 100), (c.id, K.puzzle_hash, 100),
                                         (c.id, inf_ph, 100), (c.id, K2.puzzle_hash, 100)])
    w.post(r)
    w.advance_to(w.h + w.cfg.dispute_blocks)
    return w, c, r, K, K2, alice, bob, inf_ph


def claim_push(w, r, c, ph, auth, data, builders=(), keys=()):
    a, u = w.claim_action(r, c.id, ph, auth=auth, auth_data=data)
    w.push([a], list(builders), slot_updates=u, secret_keys=list(keys))
    return "paid"


w, c, r, K, K2, alice, bob, inf_ph = setup()

# 1. wrong-width / non-canonical public keys in AUTH_SIGNATURE
for n in (0, 1, 47, 49, 96):
    attempt(f"sig-auth pk of {n} bytes for K", claim_push, w, r, c, K.puzzle_hash, AUTH_SIGNATURE, b"\x02" * n,
            expect="refuse")
attempt("sig-auth pk as a pair", claim_push, w, r, c, K.puzzle_hash, AUTH_SIGNATURE, (K.pk.to_bytes(), b""),
        keys=[K.sk], expect="refuse")
attempt("sig-auth: K2's key named for K's claim", claim_push, w, r, c, K.puzzle_hash, AUTH_SIGNATURE,
        K2.pk.to_bytes(), keys=[K2.sk], expect="refuse")
attempt("control: K signs its own claim", claim_push, w, r, c, K.puzzle_hash, AUTH_SIGNATURE, K.pk.to_bytes(),
        keys=[K.sk])

# 2. infinity key: the address standard(infinity) is anyone-can-spend anyway; does AGG_SIG_ME(inf) pass?
attempt("sig-auth with G1 infinity key, no signature", claim_push, w, r, c, inf_ph, AUTH_SIGNATURE, INF,
        expect="refuse")

# 3. non-canonical auth atoms (with the holder's real message present, so only `auth` can refuse)
for bad in (b"\x00", b"\x00\x01", b"\x01\x00", b"\xff", 3):
    a, u = w.claim_action(r, c.id, bob, auth=bad)
    attempt(f"msg-auth with auth atom {bad!r}", w.push, [a], [w.holder_message("bob", c.id, bob, 100)],
            slot_updates=u, expect="refuse")

# 4. message from a CAT coin whose inner puzzle hash is bob's address
asset = b"\x77" * 32
info = CatInfo(asset, None, bob)
gp = b"\x99" * 32
parent = Coin(gp, info.puzzle_hash(), 5)
cc = Coin(parent.coin_id(), info.puzzle_hash(), 5)
w.sim.insert_coin(cc)
cat = Cat(cc, LineageProof(gp, bob, 5), info)
leaf = claim_leaf(w.launcher_id, c.id, bob, 100)


def cat_sender():
    conds = [[66, 23, b"k" + leaf, w.d.coin.coin_id()], [51, bob, 5]]
    w.clvm.spend_cats([CatSpend(cat, Spend(w.keys.puzzle("bob"), to_program(w.clvm, conds)))])
    return w.clvm.coin_spends()


from flpcr.chain.values import to_program
a, u = w.claim_action(r, c.id, bob, auth=AUTH_MESSAGE)
attempt("msg-auth sent by a CAT coin with inner ph = bob", w.push, [a], [cat_sender], slot_updates=u,
        expect="refuse")

# 5. stale-generation message: bob's message bound to the previous singleton coin
old_coin = w.d.coin.coin_id()
attempt("control: alice claims (moves the singleton)", w.claim, r, c.id, "alice")


def stale_bob():
    coin = w.sim.new_coin(bob, 1)
    return [w.keys.spend(coin, "bob", [send_message_condition(b"k" + leaf, old_coin), [51, w.keys.ph("change"), 1]])]


a, u = w.claim_action(r, c.id, bob)
attempt("msg-auth bound to the previous generation", w.push, [a], [stale_bob], slot_updates=u, expect="refuse")

# 6. cross-leaf message reuse: bob's reclaim-shaped / delegate-shaped messages as claim auth
for nm, msg in (("reclaim message", w.reclaim_message(c.id)),
                ("delegate grant message", w.delegation_message(bob, K2.puzzle_hash, 1)),
                ("router-style c+id", None)):
    a, u = w.claim_action(r, c.id, bob)
    body = (b"c" + c.id) if msg is None else (b"k" + msg)
    attempt(f"claim authorized by bob's {nm}", w.push, [a], [lambda body=body: w.message_from("bob", body)],
            slot_updates=u, expect="refuse")
attempt("control: bob claims with the right message", w.claim, r, c.id, "bob")

# 7. AGG_SIG_ME replay across generations (manual signature)
from chia_wallet_sdk import SecretKey
GEN = {"mainnet": "ccd5bb71183532bff220ba46c268991a3ff07eb358e8255a65c30a2dce0e5fbb",
       "testnet11": "37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615"}
leaf2 = claim_leaf(w.launcher_id, c.id, K2.puzzle_hash, 100)
coin_now = w.d.coin.coin_id()
a, u = w.claim_action(r, c.id, K2.puzzle_hash, auth=AUTH_SIGNATURE, auth_data=K2.pk.to_bytes())
spends, ns, pa = w.d.spend([a])
gen_used = None
for name, g in GEN.items():
    sig = K2.sk.sign(leaf2 + coin_now + bytes.fromhex(g))
    try:
        w.sim.new_transaction(SpendBundle(spends, sig))
        gen_used = (name, g)
        w.sim.create_block() if False else None
        print(f"[as-expected] control: manual AGG_SIG_ME with {name} additional data: ACCEPTED")
        w.d.apply(ns, u)
        break
    except Exception as e:
        print(f"    manual signature with {name}: {str(e)[:100]}")
# replay: a fresh world, K2's signature over the leaf for generation N, presented at N+1
w2, c2, r2, K_, K2_, alice2, bob2, _ = setup()
leafb = claim_leaf(w2.launcher_id, c2.id, K2_.puzzle_hash, 100)
sig_old = K2_.sk.sign(leafb + w2.d.coin.coin_id() + bytes.fromhex(gen_used[1]))
w2.claim(r2, c2.id, "alice")  # singleton moves to N+1
a, u = w2.claim_action(r2, c2.id, K2_.puzzle_hash, auth=AUTH_SIGNATURE, auth_data=K2_.pk.to_bytes())
spends, ns, pa = w2.d.spend([a])
attempt("AGG_SIG_ME from generation N replayed at N+1", w2.sim.new_transaction, SpendBundle(spends, sig_old),
        expect="refuse")
sig_new = K2_.sk.sign(leafb + w2.d.coin.coin_id() + bytes.fromhex(gen_used[1]))
attempt("control: same claim re-signed for N+1", w2.sim.new_transaction, SpendBundle(spends, sig_new))

# 8. wrong-sender / wrong-prefix admin messages
w, c = world()
attempt("create with 'c'+id sent from engine_ph", w.create, c, "creator", router="engine", expect="refuse")
attempt("create with 'c'+id sent from the creator", w.create, c, "creator", router="creator", expect="refuse")
cid = c.id
sol = [w.h, w.keys.ph("creator"), c.amount, c.start, c.end, int(c.kind), c.target, c.params.as_clvm(), c.nonce,
       c.claim_window]


def dep():
    coin = w.sim.new_coin(w.keys.ph("creator"), c.amount)
    return [w.keys.spend(coin, "creator", [])]


for pre in (b"k", b"p", b"v", b"r", b""):
    attempt(f"create authorized by router message prefix {pre!r}", w.push, [Action("create", sol)],
            [lambda pre=pre: w.message_from("router", pre + cid), dep], expect="refuse")
attempt("control: create by router 'c'+id", w.create, c, "creator")
w.advance_to(c.end + 1)
rt = make_root(w, 1, c.start, c.end, [(c.id, w.keys.ph("alice"), 100)])
attempt("post_root by guardian", w.post, rt, sender="guardian", expect="refuse")
attempt("post_root by router", w.post, rt, sender="router", expect="refuse")
attempt("control: post_root by engine", w.post, rt)
attempt("veto by engine", w.veto, 1, sender="engine", expect="refuse")
attempt("veto by router", w.veto, 1, sender="router", expect="refuse")
attempt("rotate by engine", w.rotate, "mallory", sender="engine", expect="refuse")
attempt("control: veto by guardian", w.veto, 1)

# 9. delegation edge cases
w, c, r, K, K2, alice, bob, _ = setup()
cold, hot, thief = K, K2, w.sim.bls(1)
# a delegate tries to grant using its own delegation (auth=2 in the delegate leaf)
w.set_delegate(cold, hot.puzzle_hash)
sol = [w.h, cold.puzzle_hash, thief.puzzle_hash, 1, AUTH_DELEGATE, (hot.puzzle_hash, (AUTH_SIGNATURE, hot.pk.to_bytes()))]
attempt("delegate leaf with auth=2 (delegate grants a third party)", w.push, [Action("delegate", sol)],
        secret_keys=[hot.sk], expect="refuse")
# non-canonical grant, signed by the holder over the same atom
for g in (b"\x00", b"\x00\x01", b"\x01\x00"):
    sol = [w.h, cold.puzzle_hash, thief.puzzle_hash, g, AUTH_SIGNATURE, cold.pk.to_bytes()]
    attempt(f"grant atom {g!r} signed by holder", w.push, [Action("delegate", sol)], secret_keys=[cold.sk],
            expect="refuse")
# delegate of the wrong width granted by holder; it can never claim
for n in (31, 33):
    dph = b"\x12" * n
    sol = [w.h, cold.puzzle_hash, dph, 1, AUTH_SIGNATURE, cold.pk.to_bytes()]
    attempt(f"holder grants a {n}-byte delegate (puzzle accepts)", w.push, [Action("delegate", sol)],
            secret_keys=[cold.sk], delegation_updates={(cold.puzzle_hash, dph): True})
    a, u = w.claim_action(r, c.id, cold.puzzle_hash, auth=AUTH_DELEGATE, auth_data=(dph, (AUTH_MESSAGE, None)))
    attempt(f"  {n}-byte delegate claims by message", w.push, [a], slot_updates=u, expect="refuse")
# revoke and delegated claim in one spend, both orders
pair = (cold.puzzle_hash, hot.puzzle_hash)
rev = Action("delegate", [w.h, cold.puzzle_hash, hot.puzzle_hash, 0, AUTH_SIGNATURE, cold.pk.to_bytes()])
a, u = w.claim_action(r, c.id, cold.puzzle_hash, auth=AUTH_DELEGATE,
                      auth_data=(hot.puzzle_hash, (AUTH_SIGNATURE, hot.pk.to_bytes())))
attempt("one spend: [revoke, delegated claim]", w.push, [rev, a], slot_updates=u, secret_keys=[cold.sk, hot.sk],
        expect="refuse")
a, u = w.claim_action(r, c.id, cold.puzzle_hash, auth=AUTH_DELEGATE,
                      auth_data=(hot.puzzle_hash, (AUTH_SIGNATURE, hot.pk.to_bytes())))
rev = Action("delegate", [w.h, cold.puzzle_hash, hot.puzzle_hash, 0, AUTH_SIGNATURE, cold.pk.to_bytes()])
# For the second order the slot the revoke spends is the one the claim just recreated (ephemeral);
# the driver cannot express that, so build the spends by hand.
spends, ns, pa = w.d.spend([a, rev])
# the driver added one delegation slot spend (the live slot). Add the ephemeral slot spend.
from chia_wallet_sdk import Coin as _C, CoinSpend
inner_ph = w.d.inner_puzzle(w.d.state).tree_hash()
dpz = w.d.delegation_puzzle(*pair)
eph = _C(w.d.coin.coin_id(), dpz.tree_hash(), 0)
spends = [s for s in spends]
spends.append(CoinSpend(eph, dpz.serialize(), to_program(w.clvm, ((w.d.coin.parent_coin_info, (inner_ph, 1)), inner_ph)).serialize()))
attempt("one spend: [delegated claim, revoke] (claim lands, slot gone)", w.sim.spend_coins, spends,
        [cold.sk, hot.sk])
print("    unspent delegation slots after:", len(w.sim.unspent_coins(dpz.tree_hash(), False)))

# 10. reclaim: a holder's claim message / stranger's message cannot reclaim; wrong-width campaign id
w, c = world()
w.create(c, "creator")
w.advance_to(c.end + WINDOW)
for n in (0, 31, 33):
    attempt(f"reclaim with {n}-byte campaign id", w.push, [Action("reclaim", [w.h, b"\x01" * n, AUTH_MESSAGE, None])],
            expect="refuse")
sol = [w.h, c.id, AUTH_MESSAGE, None]
attempt("reclaim authorized by creator's 'k'+claim-leaf-shaped msg", w.push, [Action("reclaim", sol)],
        [lambda: w.message_from("creator", b"k" + claim_leaf(w.launcher_id, c.id, w.keys.ph("creator"), 970000))],
        expect="refuse")
attempt("control: creator reclaims", w.reclaim, c.id)

print("\nSUMMARY:", sum(1 for x in RESULTS if x[3]), "as expected,", sum(1 for x in RESULTS if not x[3]), "unexpected")
for x in RESULTS:
    if not x[3]:
        print("  UNEXPECTED:", x[0], x[1], x[2])
