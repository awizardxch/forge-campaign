"""Reviewer C probes, part 2: table never drains, duplicate claim outputs, reserve/CAT ring.
Run: PYTHONPATH=<repo> python probe_c2.py [xch|cat]"""
import sys
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from probe_lib import (outcome, invariant, world, raw, setup_campaign, src_for, make_root, ASSET, POOL, WINDOW,
                     PARAMS)
from flpcr.chain.driver import Action, Distributor, AUTH_MESSAGE
from flpcr.chain.values import to_program
from chia_wallet_sdk import CatSpend, Spend, Cat, CatInfo, Coin, LineageProof, CoinSpend

holders = (("alice", 3), ("bob", 1))

print("\n=== part 2,", "XCH" if ASSET == bytes(32) else "CAT", "===")
# ---- Q1 a fully claimed campaign keeps its table entry until the creator reclaims ------
print("-- Q1 honest table fill: fully-claimed campaigns never leave unless the creator reclaims")
w = world(max_campaigns=2)
c1 = setup_campaign(w, nonce=1)
r = make_root(w, [c1], src_for(w, holders), c1.end)
w.post(r); w.advance_to(w.h + w.cfg.dispute_blocks)
w.claim(r, c1.id, "alice"); w.claim(r, c1.id, "bob")
st = w.d.read_state()["campaigns"][c1.id]
print(f"    c1 budget={st['budget']} claimed={st['claimed']} (nothing left, still in table)")
c2 = setup_campaign(w, nonce=2)
outcome("third create with table full (c1 fully paid out)", setup_campaign, w, nonce=3)
w.advance_to(c1.end + WINDOW + 1000)
outcome("third create long after c1's window", setup_campaign, w, nonce=4)
# only the creator can free it
rec = Action("reclaim", [w.h, c1.id, AUTH_MESSAGE, None])
outcome("stranger frees c1 (message from 'stranger')", w.push, [rec],
        [lambda: w.message_from("stranger", b"k" + w.reclaim_message(c1.id))])
outcome("creator reclaims 0 leftovers", w.reclaim, c1.id)
outcome("third create after creator reclaim", setup_campaign, w, nonce=5)

# ---- Q2 one holder, two campaigns, equal payouts, one spend -----------------------------
print("-- Q2 batched claims with equal (ph, amount)")
w = world()
cA = setup_campaign(w, nonce=1)
cB = setup_campaign(w, nonce=2)
r = make_root(w, [cA, cB], src_for(w, holders), cA.end)
w.post(r); w.advance_to(w.h + w.cfg.dispute_blocks)
alice = w.keys.ph("alice")
print(f"    alice cumulative A={r.claim_for(cA.id, alice).cumulative} B={r.claim_for(cB.id, alice).cumulative}")
aA, uA = w.claim_action(r, cA.id, alice)
# apply A's slot update to the driver's view virtually for B's neighbour lookup: B's key is different, so its
# neighbours come from the current list unless A's key sits between them.
aB, uB = w.claim_action(r, cB.id, alice)
kA = Distributor.claim_key(cA.id, alice)
kB = Distributor.claim_key(cB.id, alice)
print(f"    keys adjacent in insertion? A<B={kA < kB}")
mA = w.holder_message("alice", cA.id, alice, aA.solution[3])
mB = w.holder_message("alice", cB.id, alice, aB.solution[3])
ups = dict(uA); ups.update(uB)
outcome("alice claims A and B (equal amounts) in one spend", w.push, [aA, aB], [mA, mB], slot_updates=ups)
# control: separately
outcome("control: alice claims A alone", w.claim, r, cA.id, "alice")
outcome("control: alice claims B alone", w.claim, r, cB.id, "alice")
invariant(w)

# ---- Q3 reserve spent without / against the singleton ----------------------------------
print("-- Q3 reserve spends")
w = world()
c = setup_campaign(w)
d = w.d
thief = w.keys.ph("thief")
reserve_amt = w.reserve_on_chain()
fake_inner = d.inner_puzzle(d.state).tree_hash()
delegated = w.clvm.pair(w.clvm.atom(b"\x01"), to_program(w.clvm, [[51, thief, reserve_amt, [thief]]]))
sol = to_program(w.clvm, [fake_inner, delegated])
def lone_reserve_spend():
    if d.is_cat:
        w.clvm.spend_cats([CatSpend(d.reserve_cat, Spend(d.reserve_puzzle, sol))])
        w.sim.spend_coins(w.clvm.coin_spends(), [])
    else:
        w.sim.spend_coins([CoinSpend(d.reserve_coin, d.reserve_puzzle.serialize(), sol.serialize())], [])
outcome("reserve spent alone, paying thief", lone_reserve_spend)

# honest singleton action (rotate) but reserve delegated puzzle swapped to pay thief
def swapped():
    new_engine = w.keys.ph("engine2")
    spends, new_state, _ = d.spend([Action("rotate", [w.h, new_engine])])
    out = []
    for s in spends:
        if s.coin.coin_id() == d.reserve_coin.coin_id() and not d.is_cat:
            out.append(CoinSpend(s.coin, s.puzzle_reveal, sol.serialize()))
        else:
            out.append(s)
    if d.is_cat:  # rebuild the CAT spend with the thief's delegated puzzle
        out = [s for s in spends if s.coin.coin_id() != d.reserve_coin.coin_id()]
        w.clvm.spend_cats([CatSpend(d.reserve_cat, Spend(d.reserve_puzzle, sol))])
        out.extend(w.clvm.coin_spends())
    out.extend(w.message_from("guardian", b"r" + new_engine))
    w.sim.spend_coins(out, [])
outcome("rotate + reserve delegated puzzle paying thief", swapped)
invariant(w)

# ---- Q4 CAT ring --------------------------------------------------------------------------
if d.is_cat:
    print("-- Q4 CAT ring")
    from flpcr.clvm import sha256tree
    def other_asset_cat(label, amount, asset=b"\x77" * 32):
        ph = w.keys.ph(label)
        info = CatInfo(asset, None, ph)
        gp = w.clvm.atom(label.encode() + amount.to_bytes(8, "big")).tree_hash()
        parent = Coin(gp, info.puzzle_hash(), amount)
        coin = Coin(parent.coin_id(), info.puzzle_hash(), amount)
        w.sim.insert_coin(coin)
        return Cat(coin, LineageProof(gp, ph, amount), info)

    def create_with(deposit_cat, amount=1000, nonce=50):
        cph = w.keys.ph("creator")
        st, en = w.h + 2, w.h + 40
        s = [w.h, cph, amount, st, en, 1, POOL, list(PARAMS), nonce, WINDOW]
        cid = sha256tree(["flpcr-campaign-v1", w.launcher_id, cph, w.cfg.reward_asset, amount, st, en, 1, POOL,
                          sha256tree(list(PARAMS)), nonce, w.cfg.fee_bps, WINDOW])
        return w.push([Action("create", s)], [lambda: w.message_from("router", b"c" + cid)],
                      cat_spends=[CatSpend(deposit_cat, Spend(w.keys.puzzle("creator"), w.clvm.nil()))])
    outcome("create, deposit of another CAT asset in the ring", create_with, other_asset_cat("creator", 1000))
    invariant(w)
    # siphon: claim spend + attacker's own ring coin that outputs more than it put in
    r = make_root(w, [c], src_for(w, holders), c.end)
    w.post(r); w.advance_to(w.h + w.cfg.dispute_blocks)
    alice = w.keys.ph("alice")
    a, u = w.claim_action(r, c.id, alice)
    m = w.holder_message("alice", c.id, alice, a.solution[3])
    att = w.mint_cat("thief", 10)
    tp = w.keys.ph("thief")
    def siphon(extra):
        return w.push([a], [m], slot_updates=u,
                      cat_spends=[CatSpend(att, Spend(w.keys.puzzle("thief"), to_program(w.clvm, [[51, tp, 10 + extra, [tp]]])))])
    outcome("claim + thief ring coin outputs +5000", siphon, 5000)
    outcome("control: claim + thief ring coin outputs exactly its own 10", siphon, 0)
    invariant(w)
    # create with an oversupplied deposit: excess beyond amount
    outcome("create with deposit amount+500 (excess in ring)", create_with, w.mint_cat("creator", 1500), 1000, 51)
    invariant(w)
