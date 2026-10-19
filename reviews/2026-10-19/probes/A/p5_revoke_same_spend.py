"""Revoke and the revoked delegate's claim in one singleton spend. The driver emits one slot spend per
SEND_MESSAGE, so it duplicates the live slot; dedupe by coin id and add the ephemeral slot by hand."""
from chia_wallet_sdk import Coin, CoinSpend
from flpcr.chain.values import to_program
from common import *


def fresh():
    w, c = world()
    w.create(c, "creator")
    cold, hot = w.sim.bls(1), w.sim.bls(1)
    w.advance_to(c.end + 1)
    r = make_root(w, 1, c.start, c.end, [(c.id, cold.puzzle_hash, 100)])
    w.post(r)
    w.advance_to(w.h + w.cfg.dispute_blocks)
    w.set_delegate(cold, hot.puzzle_hash)
    return w, c, r, cold, hot


def build(w, r, c, cold, hot, order, ephemeral):
    a, u = w.claim_action(r, c.id, cold.puzzle_hash, auth=AUTH_DELEGATE,
                          auth_data=(hot.puzzle_hash, (AUTH_SIGNATURE, hot.pk.to_bytes())))
    rev = Action("delegate", [w.h, cold.puzzle_hash, hot.puzzle_hash, 0, AUTH_SIGNATURE, cold.pk.to_bytes()])
    acts = [a, rev] if order == "claim-first" else [rev, a]
    spends, ns, pa = w.d.spend(acts)
    seen, out = set(), []
    for s in spends:
        k = s.coin.coin_id()
        if k not in seen:
            seen.add(k)
            out.append(s)
    if ephemeral:
        inner_ph = w.d.inner_puzzle(w.d.state).tree_hash()
        dpz = w.d.delegation_puzzle(cold.puzzle_hash, hot.puzzle_hash)
        eph = Coin(w.d.coin.coin_id(), dpz.tree_hash(), 0)
        sol = to_program(w.clvm, ((w.d.coin.parent_coin_info, (inner_ph, 1)), inner_ph))
        out.append(CoinSpend(eph, dpz.serialize(), sol.serialize()))
    return out


for order in ("revoke-first", "claim-first"):
    for eph in (False, True):
        w, c, r, cold, hot = fresh()
        before = w.balance(cold.puzzle_hash)
        spends = build(w, r, c, cold, hot, order, eph)
        ok = attempt(f"one spend [{order}], ephemeral slot spend={eph}", w.sim.spend_coins, spends,
                     [cold.sk, hot.sk], expect="accept" if eph else "refuse")
        dpz = w.d.delegation_puzzle(cold.puzzle_hash, hot.puzzle_hash)
        print(f"    paid to holder: {w.balance(cold.puzzle_hash) - before}; delegation slots left: "
              f"{len(w.sim.unspent_coins(dpz.tree_hash(), False))}")
