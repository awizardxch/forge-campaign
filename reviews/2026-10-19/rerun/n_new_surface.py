"""New surface since the review, probed for the same attack classes.

A. The ephemeral-slot trick against the NEW delegation slot list.
B. Claims with AUTH_SIGNATURE (AGG_SIG_UNSAFE, generation-free): replayed
   signatures, signatures reused for another cumulative, excluded addresses.
C. The bond coin and the dispute / resolve leaves.

Attacks are expected REFUSED. Three in section C were ACCEPTED on 6b8c027 and
are fixed on fix-bond-reclaim (see the README and FINDINGS.md status):
  C5 NEW-1  anyone who saw a dispute bundle stole the whole bond: the bond
            launcher was anyone-can-spend and its destination came from its
            solution. Now the launcher is curried with the bond puzzle hash,
            the distributor id and disputer_ph, and creates the bond only on
            the dispute leaf's message.
  C6 NEW-2  ... or kept the dispute but redirected its refund: `disputer_ph`
            was a free solution field. Now the leaf derives the launcher's id
            from it, so a rewrite names a coin nobody created.
  C7 NEW-3  the creator disputed the final root just before it activated and
            reclaimed once the claim window opened. Now reclaim.rue refuses
            while a dispute is open or the pending root raises the total.
The script exits non-zero if any gets through."""

from chia_wallet_sdk import Coin, CoinSpend, Signature, SpendBundle

from flpcr.chain.signer import ADDITIONAL_DATA, KeySigner
from flpcr.merkle import build_tree, claim_leaf
from flpcr.engine import Root

from common import (ASSETS, Action, LeafCall, ByDelegate, BySignature, Distributor, DelegationRecord, attempt, check,
                    ephemeral_slot, final_root, finish, settle, src, to_program, world)

SIM_DATA = ADDITIONAL_DATA["testnet11"]  # the simulator's AGG_SIG_ME additional data


def section_a(tag, asset):
    print(f"\n{tag} A: ephemeral slots on the delegation list")
    w, c = world(asset)
    cold, hot, thief = w.bls(), w.bls(), w.bls()
    w.create(c, "creator")
    r = final_root(w, c, src((cold.puzzle_hash, 1), (w.keys.ph("bob"), 1)))
    w.post(r)
    settle(w)
    w.set_delegate(cold, hot.puzzle_hash)
    w.set_delegate(cold, hot.puzzle_hash, grant=0)  # revoked: the slot is (key, 0, uses, l, r)
    key = Distributor.delegation_key(cold.puzzle_hash, hot.puzzle_hash)
    rec = w.d.delegations[key]

    def revoked_claim(eph):
        call = w.claim_call(r, c.id, cold.puzzle_hash, auth=ByDelegate(hot.puzzle_hash, BySignature(hot.pk.to_bytes())))
        extra = []
        if eph is not None:
            active, uses = eph
            puzzle = w.d.delegation_slot_puzzle(key, DelegationRecord(active, uses, rec.left, rec.right, b"", b""))
            extra = [lambda: [ephemeral_slot(w, puzzle)]]
        return w.push([call], extra)

    attempt("revoked delegate claims", revoked_claim, None, expect="refuse", reason="MessageNotSentOrReceived")
    attempt("ATTACK revoked delegate + ephemeral spend of the slot this claim creates (1, uses+1)",
            revoked_claim, (1, rec.uses + 1), expect="refuse", reason="MessageNotSentOrReceived")
    attempt("ATTACK revoked delegate + 'ephemeral' slot at the value the leaf spends (1, uses)",
            revoked_claim, (1, rec.uses), expect="refuse", reason="UnknownUnspent")

    # A slot value that existed once (before the revoke): uses - 1, active 1. Its coin is spent.
    def stale_uses():
        call = w.claim_call(r, c.id, cold.puzzle_hash, auth=ByDelegate(hot.puzzle_hash, BySignature(hot.pk.to_bytes())))
        d = call.action.solution[8]
        call.action.solution[8] = (d[0], (rec.uses - 1, d[1][1]))
        return w.push([call])
    attempt("ATTACK replay the pre-revoke slot value (active 1, uses - 1)", stale_uses, expect="refuse",
            reason="MessageNotSentOrReceived")

    # The thief's own grant in the same spend recreates neighbours (keeping their active/uses), so it
    # cannot turn the victim's revoked slot active.
    def with_own_grant():
        grant = w.d.delegate(w.h, thief.puzzle_hash, w.keys.ph("x"), 1, BySignature(thief.pk.to_bytes()))
        call = w.claim_call(r, c.id, cold.puzzle_hash, auth=ByDelegate(hot.puzzle_hash, BySignature(hot.pk.to_bytes())))
        saved = w.d._check_collisions
        w.d._check_collisions = lambda *a: None  # past the driver: what consensus says
        try:
            return w.push([grant, call])
        finally:
            w.d._check_collisions = saved
    # The thief's new key lands next to the victim's in this small list, so its insert recreates the
    # victim's slot (with right = the new key, active still 0); the claim then names a slot that is
    # neither the spent nor the recreated value.
    attempt("ATTACK the attacker's own grant in the same spend + the revoked delegate's claim (driver check off)",
            with_own_grant, expect="refuse", reason="MessageNotSentOrReceived")
    w.set_delegate(cold, hot.puzzle_hash)
    attempt("control: re-granted delegate claims (paid to holder)", w.claim_delegated, r, c.id, cold.puzzle_hash, hot,
            signed=True)


def section_b(tag, asset):
    print(f"\n{tag} B: AUTH_SIGNATURE claims and excluded addresses")
    w, c = world(asset)
    wal = w.bls()
    w.create(c, "creator")
    s = src((wal.puzzle_hash, 1), (w.keys.ph("bob"), 1))
    from flpcr import EngineState, run_epoch
    w.advance_to(c.start + 21)
    st, r1 = run_epoch(EngineState(w.launcher_id, height=c.start), [c], s, c.start + 20)
    w.post(r1)
    settle(w)
    signer = KeySigner([wal.sk], SIM_DATA)
    call = w.claim_call(r1, c.id, wal.puzzle_hash, auth=BySignature(wal.pk.to_bytes()))
    prepared = w.d.prepare([call])
    sig1 = signer.sign(list(prepared.spends))  # the holder's AGG_SIG_UNSAFE over leaf(r1 cumulative)
    before = w.balance(wal.puzzle_hash)
    attempt("control: a third party submits the holder's signed claim (no coin at the address)",
            lambda: (w.sim.new_transaction(SpendBundle(list(prepared.spends), sig1)), w.d.apply(prepared)))
    check("  ... paid to the holder", w.balance(wal.puzzle_hash) - before == r1.claim_for(c.id, wal.puzzle_hash).cumulative)
    replay = w.claim_call(r1, c.id, wal.puzzle_hash, auth=BySignature(wal.pk.to_bytes()))
    attempt("ATTACK replay the same signature and leaf after it was paid", w.d.prepare, [replay], expect="refuse",
            reason="clvm raise")  # claim.rue `assert amount > 0`: cumulative == claimed
    w.advance_to(c.end + 1)
    _, r2 = run_epoch(st, [c], s, c.end)
    w.post(r2)
    settle(w)
    call2 = w.claim_call(r2, c.id, wal.puzzle_hash, auth=BySignature(wal.pk.to_bytes()))
    p2 = w.d.prepare([call2])
    attempt("ATTACK the r1 signature reused for the r2 cumulative", w.sim.new_transaction,
            SpendBundle(list(p2.spends), sig1), expect="refuse", reason="BadAggregateSignature")
    other = w.bls()
    attempt("ATTACK another key's signature for the holder's leaf", w.sim.new_transaction,
            SpendBundle(list(p2.spends), Signature.aggregate([other.sk.sign(p2.signatures[0].message)])),
            expect="refuse", reason="BadAggregateSignature")

    # Excluded addresses: the creator excludes one; a root (forged by the operator) pays it anyway.
    from flpcr import Kind, Params
    from common import POOL, LEAD
    w, _ = world(asset)
    ex = w.bls()
    c = w.campaign("creator", 1_000_000, w.h + LEAD + 2, w.h + LEAD + 42, Kind.LP_HOLD, POOL, claim_window=160,
                   params=Params(blacklist=frozenset({ex.puzzle_hash})))
    w.create(c, "creator")
    burn = bytes(30) + b"\xde\xad"
    rows = [(c.id, ex.puzzle_hash, 1000), (c.id, burn, 1000), (c.id, w.keys.ph("bob"), 1000)]
    leaves = [claim_leaf(w.launcher_id, cid, ph, n) for cid, ph, n in rows]
    root, proofs = build_tree(leaves)
    from flpcr.engine import Claim
    forged = Root(w.launcher_id, 1, c.start, c.end, root, {c.id: 3000},
                  [Claim(cid, ph, n, leaf, proofs[leaf]) for (cid, ph, n), leaf in zip(rows, leaves)])
    w.advance_to(c.end + 1)
    w.post(forged)
    settle(w)
    attempt("ATTACK excluded address claims by key signature from a root that pays it",
            lambda: w.push([w.claim_call(forged, c.id, ex.puzzle_hash, auth=BySignature(ex.pk.to_bytes()))]),
            expect="refuse", reason="clvm raise")  # `!is_member(excluded, puzzle_hash)`
    attempt("ATTACK excluded address claims with an empty excluded list",
            lambda: w.push([w.d.claim(w.h, c.id, ex.puzzle_hash, 1000, forged.claim_for(c.id, ex.puzzle_hash).proof,
                                      BySignature(ex.pk.to_bytes()), excluded=[])]),
            expect="refuse", reason="clvm raise")  # `tree_hash(excluded) == campaign.excluded`
    hot = w.bls()
    w.set_delegate(ex, hot.puzzle_hash)
    attempt("ATTACK excluded address claims through a delegate",
            w.claim_delegated, forged, c.id, ex.puzzle_hash, hot, signed=True, expect="refuse", reason="clvm raise")
    attempt("ATTACK the burn address claims",
            lambda: w.push([w.claim_call(forged, c.id, burn)]), expect="refuse", reason="clvm raise")
    attempt("control: a listed holder claims from the same root", w.claim, forged, c.id, "bob")


def honest_dispute_bundle(w, root_id, disputer):
    """The disputer's coin spend (a standard wallet) creating the bond launcher, signed;
    and the driver's spends for the dispute. What the disputer publishes."""
    bond = w.cfg.dispute_bond
    cl = w.clvm
    coin = disputer.coin
    launcher_ph = w.d.bond_launcher_ph(disputer.puzzle_hash)
    cl.spend_standard_coin(coin, disputer.pk, cl.delegated_spend([
        to_program(cl, [51, launcher_ph, bond, [w.launcher_id]]),
        to_program(cl, [51, disputer.puzzle_hash, coin.amount - bond]),
    ]))
    funding = list(cl.coin_spends())
    sig = KeySigner([disputer.sk], SIM_DATA).sign(funding)
    launcher = Coin(coin.coin_id(), launcher_ph, bond)
    return funding, sig, launcher


def section_c(tag, asset):
    print(f"\n{tag} C: bonds, disputes, resolves")
    w, c = world(asset)
    bond = w.cfg.dispute_bond
    w.create(c, "creator")
    r = final_root(w, c, src((w.keys.ph("alice"), 1)))
    w.post(r)
    disputer = w.bls(bond + 10)
    funding, sig, launcher = honest_dispute_bundle(w, r.root_id, disputer)

    call = w.d.dispute(w.h, r.root_id, disputer.puzzle_hash, disputer.coin.coin_id())
    p = w.d.prepare([call])
    attempt("C1 dispute with no bond launcher in the bundle", w.sim.new_transaction,
            SpendBundle([s for s in p.spends if s.coin.coin_id() != launcher.coin_id()], Signature.infinity()),
            expect="refuse", reason="MessageNotSentOrReceived")
    wrong = CoinSpend(launcher, w.d.bond_launcher_puzzle(disputer.puzzle_hash).serialize(),
                      to_program(w.clvm, [bond, b"\x42" * 32]).serialize())
    attempt("C2 dispute whose launcher names another sender (not this distributor's generation)", w.sim.new_transaction,
            SpendBundle([s for s in p.spends if s.coin.coin_id() != launcher.coin_id()] + [wrong] + funding, sig),
            expect="refuse", reason="MessageNotSentOrReceived")

    # C5: the thief takes the published funding spend and signature, and spends the launcher to itself.
    thief = w.keys.ph("thief")
    evil = CoinSpend(launcher, w.d.bond_launcher_puzzle(thief).serialize(),
                     next(s for s in p.spends if s.coin.coin_id() == launcher.coin_id()).solution)
    before = w.xch_balance(thief)
    attempt("C5 ATTACK (NEW-1) reuse the disputer's signed funding spend; a launcher that pays the thief",
            w.sim.new_transaction, SpendBundle(funding + [evil], sig), expect="refuse", reason="WrongPuzzleHash")
    alone = [s for s in p.spends if s.coin.coin_id() == launcher.coin_id()]
    attempt("C5b ATTACK (NEW-1) reuse the funding spend and spend the launcher alone (strand the bond)",
            w.sim.new_transaction, SpendBundle(funding + alone, sig), expect="refuse",
            reason="MessageNotSentOrReceived")
    stolen = w.xch_balance(thief) - before
    check(f"C5 the thief holds none of the disputer's bond", stolen == 0, f"(thief received {stolen} of {bond})")

    # C6: same published spends, but the dispute names the thief as refund address; the guardian upholds.
    w, c = world(asset)
    w.create(c, "creator")
    r = final_root(w, c, src((w.keys.ph("alice"), 1)))
    w.post(r)
    disputer = w.bls(bond + 10)
    funding, sig, launcher = honest_dispute_bundle(w, r.root_id, disputer)
    call = w.d.dispute(w.h, r.root_id, disputer.puzzle_hash, disputer.coin.coin_id())
    call.action.solution[2] = thief  # the leaf's refund address; the launcher spend stays as published
    rewritten = w.d.prepare([call])

    def redirect():
        w.sim.new_transaction(SpendBundle(list(rewritten.spends) + funding, sig))
        w.d.apply(rewritten)
        return w.state().dispute.disputer_ph == thief
    attempt("C6 ATTACK (NEW-2) the disputer's published bundle rewritten with disputer_ph = thief",
            redirect, expect="refuse", reason="MessageNotSentOrReceived")
    if w.state().dispute is not None:
        before = w.xch_balance(thief)
        w.resolve(1)
        got = w.xch_balance(thief) - before
        check("C6 an upheld dispute refunds the disputer, not the rewriter", got == 0,
              f"(thief received {got}; the disputer funded {bond})")

    # C3/C4 on an honest dispute: the bond coin moves only by resolve's message, to the leaf's recipient.
    w, c = world(asset)
    w.create(c, "creator")
    r = final_root(w, c, src((w.keys.ph("alice"), 1)))
    w.post(r)
    w.dispute(r.root_id)
    st = w.state().dispute
    bond_coin = Coin(st.bond_parent, w.d.bond_ph, bond)
    delegated = to_program(w.clvm, (1, [[51, thief, bond, [thief]]]))
    sol = to_program(w.clvm, [w.d.inner_puzzle(w.d.state).tree_hash(), delegated])
    steal = CoinSpend(bond_coin, w.d.bond_puzzle.serialize(), sol.serialize())
    attempt("C3 spend the bond coin to the thief without resolve", w.sim.spend_coins, [steal], [], expect="refuse",
            reason="MessageNotSentOrReceived")
    call = w.d.resolve(w.h, 1)
    call.extra_spends = [steal]
    attempt("C4 resolve (upheld) with the bond coin's puzzle swapped to pay the thief", w.push, [call],
            expect="refuse", reason="MessageNotSentOrReceived")
    attempt("C4 control: resolve (upheld) as built", w.resolve, 1)
    attempt("C8 a second resolve with no dispute open",
            lambda: w.push([LeafCall(Action("resolve", [w.h, 1, w.cfg.guardian_ph]), [])]),
            expect="refuse", reason="clvm raise")  # resolve.rue `assert !(state.dispute is nil)`, its first check

    # C7: the creator disputes the final root just before it activates and reclaims when the window opens.
    w, c = world(asset)
    w.create(c, "creator")
    alice = w.keys.ph("alice")
    r = final_root(w, c, src((alice, 1)))
    w.post(r)
    w.advance_to(w.state().pending.activates_at - 1)
    w.dispute(r.root_id, disputer="creator")
    w.advance_to(c.end + c.claim_window)
    got = attempt(f"C7 ATTACK (NEW-3) creator disputes its final root, then reclaims at end + window "
                  f"({c.claim_window}) while the root is disputed", w.reclaim, c.id, expect="refuse",
                  reason="clvm raise")  # reclaim.rue `assert state.dispute is nil`
    if got:
        print(f"      the creator took {got}, alice's whole accrual, for a forfeited bond of {bond}")
    else:
        w.resolve(0)  # the guardian rejects the dispute; the bond is forfeited, the window restarts
        attempt("C7 ATTACK (NEW-3) ... and reclaims while the restarted root is pending", w.reclaim, c.id,
                expect="refuse", reason="clvm raise")  # reclaim.rue: the pending root raises the total
        settle(w)
        got = attempt("C7 control: alice claims the final root once it activates", w.claim, r, c.id, "alice")
        check("C7 alice was paid her accrual", bool(got), f"({got})")
        attempt("C7 control: the creator reclaims what is left", w.reclaim, c.id)

for tag, asset in ASSETS:
    section_a(tag, asset)
    section_b(tag, asset)
    section_c(tag, asset)
finish("new surface")
