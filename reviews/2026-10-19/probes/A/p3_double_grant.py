"""A-3: two grants of the same (holder, delegate) create two delegation slot coins at the same
puzzle hash; one revoke spends one of them and the delegate keeps claiming with the other."""
from common import *

for asset in (bytes(32), b"\xa5" * 32):
    print(f"--- reward asset {'XCH' if asset == bytes(32) else 'CAT'}")
    w, c = world(asset)
    cold, hot = w.sim.bls(1), w.sim.bls(1)
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, cold.puzzle_hash, 1)]})
    w.create(c, "creator")
    w.advance_to(c.end + 1)
    _, r = engine_root(w, c, src, c.end)
    w.post(r)
    w.advance_to(w.h + w.cfg.dispute_blocks)
    pair = (cold.puzzle_hash, hot.puzzle_hash)

    # Control: grant once, revoke once -> delegate refused.
    snapshot_state = None
    w.set_delegate(cold, hot.puzzle_hash)
    w.set_delegate(cold, hot.puzzle_hash, grant=0)
    attempt("control: single grant, single revoke, delegate claims", w.claim_delegated, r, c.id, cold.puzzle_hash,
            hot, expect="refuse")

    # Attack: the holder grants twice (e.g. a UI retry), then revokes once.
    attempt("second-generation grant #1", w.set_delegate, cold, hot.puzzle_hash)
    first = w.d.delegations[pair]
    attempt("grant #2 (redundant, not refused)", w.set_delegate, cold, hot.puzzle_hash)
    attempt("revoke (holder signs grant=0)", w.set_delegate, cold, hot.puzzle_hash, grant=0)
    print("    driver believes delegation present:", pair in w.d.delegations)
    # The first slot coin is still unspent on chain; its lineage is public (hinted with the holder).
    slot_ph = w.d.delegation_puzzle(*pair).tree_hash()
    live = w.sim.unspent_coins(slot_ph, False)
    print("    unspent delegation slot coins at that puzzle hash:", len(live))
    w.d.delegations[pair] = first
    got = attempt("ATTACK: revoked delegate claims via the surviving slot", w.claim_delegated, r, c.id,
                  cold.puzzle_hash, hot, expect="refuse")
