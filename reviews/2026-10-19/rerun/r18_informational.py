"""Informational items with a probe (FINDINGS.md § Informational).

C-3  CAT deposit excess (old C/probe_c2.py Q4): a CAT create whose deposit
     exceeds `amount` with no change output is refused by the CAT ring
     (conservation inside the ring); with a change output it is accepted. Not
     changed by design: the caller's CatSpend must carry the change.
B-2  Decoy slots (reasoning only in the review): a 0-mojo coin at a live
     claim slot's puzzle hash, hinted the same, cannot stand in for the slot
     (the slot puzzle asserts its parent is a singleton generation:
     AssertMyParentIdFailed), and resync (flpcr/chain/resync.py), which replays
     singleton generations instead of scanning hints, ignores it.
D-6  The late veto is in r03_dispute_window.py; E-7 (blacklisted wrapper) in
     r01_09_17_engine.py; the NFT melt gap in r10_nft_reader.py."""

from chia_wallet_sdk import CatSpend, Clvm, Coin, CoinSpend, Spend

from flpcr.chain.driver import Puzzles
from flpcr.chain.resync import resync

from common import CAT_ASSET, Distributor, attempt, check, final_root, finish, settle, src, to_program, world

print("\nC-3 CAT deposit excess")
w, c = world(CAT_ASSET)


def create_with(extra, change):
    call = w.d.create(w.h, w.keys.ph("creator"), c.amount, c.start, c.end, 1, c.target, c.params.as_clvm(), c.nonce,
                      c.claim_window)
    cat = w.mint_cat("creator", c.amount + extra)
    conds = [[51, w.keys.ph("creator"), extra, [w.keys.ph("creator")]]] if change else []
    spend = CatSpend(cat, Spend(w.keys.puzzle("creator"), to_program(w.clvm, conds)))
    return w.push([call], cat_spends=[spend])


attempt("CAT deposit of amount + 5, no change output", create_with, 5, False, expect="refuse",
        reason="AssertCoinAnnouncementFailed")
attempt("CAT deposit of amount + 5, change of 5 to the creator", create_with, 5, True)

print("\nB-2 decoy slots")
for tag, asset in (("XCH", bytes(32)), ("CAT", CAT_ASSET)):
    w, c = world(asset)
    w.create(c, "creator")
    alice = w.keys.ph("alice")
    r = final_root(w, c, src((alice, 1), (w.keys.ph("bob"), 1)))
    w.post(r)
    settle(w)
    w.claim(r, c.id, "alice")
    key = Distributor.claim_key(c.id, alice)
    rec = w.d.claims[key]
    puzzle = w.d.claim_slot_puzzle(key, rec)
    decoy_parent = w.sim.new_coin(w.keys.ph("mallory"), 1)
    w.sim.spend_coins([w.keys.spend(decoy_parent, "mallory", [[51, puzzle.tree_hash(), 0, [key]]])], [])
    decoy = Coin(decoy_parent.coin_id(), puzzle.tree_hash(), 0)
    check(f"{tag} decoy exists at the live slot's puzzle hash",
          len(w.sim.unspent_coins(puzzle.tree_hash(), False)) == 2)
    got = resync(w.sim, Puzzles(Clvm()), w.launcher_id)
    check(f"{tag} resync ignores the decoy (same claim slot record as the live driver)",
          got.distributor.snapshot() == w.d.snapshot())
    inner_ph = w.d.inner_puzzle(w.d.state).tree_hash()
    lineage = to_program(w.clvm, ((rec.creator_parent, (rec.creator_inner_ph, 1)), inner_ph))
    attempt(f"{tag} ATTACK spend the decoy as alice's slot (singleton lineage claimed)", w.sim.spend_coins,
            [CoinSpend(decoy, puzzle.serialize(), lineage.serialize())], [], expect="refuse",
            reason="AssertMyParentIdFailed")
finish("informational")
