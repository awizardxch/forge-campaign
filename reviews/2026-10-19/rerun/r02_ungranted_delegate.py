"""FLPCR-02 rerun: an ungranted key proves a delegation with the slot its own
spend creates (old probes A/a4_verify.py, A/a4_reclaim.py).

The fix: a delegated use spends the active slot (key, 1, uses, left, right)
and recreates (key, 1, uses + 1, left, right). The slot a spend creates is
never the one it spends, so spending it ephemerally proves nothing.

Covered by tests/test_chain.py::TestDelegatedClaims::
test_ungranted_delegate_cannot_prove_itself_with_this_spends_slot (claims,
both assets). This script reruns the original shape for claims AND reclaims,
and pins the consensus reason: the leaf's mode-18 message to the slot it
spends has no receiver (MessageNotSentOrReceived)."""

from common import *  # noqa: F401,F403
from common import ASSETS, ByDelegate, ByMessage, BySignature, DelegationRecord, Distributor, LOW, HIGH, \
    attempt, ephemeral_slot, final_root, finish, settle, src, world


def delegation_puzzle(w, owner, delegate, uses):
    key = Distributor.delegation_key(owner, delegate)
    return w.d.delegation_slot_puzzle(key, DelegationRecord(1, uses, LOW, HIGH, b"", b""))


def run(tag, asset):
    print(f"\n{tag}: claims")
    w, c = world(asset)
    cold, hot, thief = w.bls(), w.bls(), w.bls()
    w.create(c, "creator")
    r = final_root(w, c, src((cold.puzzle_hash, 1), (w.keys.ph("bob"), 1)))
    w.post(r)
    settle(w)

    def ungranted_claim(eph_uses):
        call = w.claim_call(r, c.id, cold.puzzle_hash, auth=ByDelegate(thief.puzzle_hash, BySignature(thief.pk.to_bytes())))
        extra = [] if eph_uses is None else [lambda: [ephemeral_slot(w, delegation_puzzle(w, cold.puzzle_hash, thief.puzzle_hash, eph_uses))]]
        before = w.balance(cold.puzzle_hash)
        w.push([call], extra)
        return w.balance(cold.puzzle_hash) - before

    attempt("ungranted delegated claim, no slot", ungranted_claim, None, expect="refuse",
            reason="MessageNotSentOrReceived")
    # The original attack: spend, in the same bundle, the slot this spend creates (uses 0 -> 1).
    attempt("ATTACK ungranted claim + ephemeral spend of the slot this spend creates (uses+1)", ungranted_claim, 1,
            expect="refuse", reason="MessageNotSentOrReceived")
    # And the value the leaf spends (uses 0): no spend creates it, so the coin does not exist.
    attempt("ATTACK ungranted claim + 'ephemeral' slot at the value the leaf spends (uses 0)", ungranted_claim, 0,
            expect="refuse", reason="UnknownUnspent")
    w.set_delegate(cold, hot.puzzle_hash)
    attempt("control: granted delegate claims for the holder (paid to holder)",
            w.claim_delegated, r, c.id, cold.puzzle_hash, hot, signed=True)

    print(f"\n{tag}: reclaims")
    w, c = world(asset)
    stranger, hot = w.bls(), w.bls()
    w.create(c, "creator")
    r = final_root(w, c, src((w.keys.ph("alice"), 1)))
    w.post(r)
    settle(w)
    w.advance_to(c.end + c.claim_window)
    creator = w.keys.ph("creator")

    def stranger_reclaim(eph_uses):
        call = w.d.reclaim(w.h, c.id, ByDelegate(stranger.puzzle_hash, BySignature(stranger.pk.to_bytes())))
        extra = [] if eph_uses is None else [lambda: [ephemeral_slot(w, delegation_puzzle(w, creator, stranger.puzzle_hash, eph_uses))]]
        before = w.balance(creator)
        w.push([call], extra)
        return w.balance(creator) - before

    attempt("stranger reclaim as creator's delegate, no slot", stranger_reclaim, None, expect="refuse",
            reason="MessageNotSentOrReceived")
    attempt("ATTACK stranger reclaim + ephemeral spend of the slot this spend creates", stranger_reclaim, 1,
            expect="refuse", reason="MessageNotSentOrReceived")
    w.set_delegate("creator", hot.puzzle_hash)
    attempt("control: creator's granted delegate reclaims (paid to creator)", w.reclaim, c.id, delegate=hot)


for tag, asset in ASSETS:
    run(tag, asset)
finish("FLPCR-02")
