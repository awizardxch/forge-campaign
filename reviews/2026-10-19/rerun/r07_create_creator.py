"""FLPCR-07 rerun: `create` bound neither the creator nor the funder, and a
malformed creator locked the budget (old probe A/p2_create_creator.py).

The fix (create.rue): the creator must spend a coin of its own at `creator`
sending "k" + id (authorize_by_spend: a signature is refused), next to the
router's "c" + id; and `assert creator.length == 32`. The same 32-byte check
went on delegate owner/delegate, claim holder, rotate key and disputer_ph.
Covered by tests/test_chain.py::TestCreatorConsents, TestClaimRefusals::
test_holder_must_be_32_bytes, TestDelegatedClaims::test_owner_and_delegate_are_32_bytes,
TestRotation::test_new_key_must_be_32_bytes, TestDisputes::test_refund_address_must_be_32_bytes.

Reasons: someone else naming the creator -> MessageNotSentOrReceived (the
creator's "k" + id is never sent; the leaf's assertions all pass, since the id
is well formed). A 0/31/33-byte creator -> clvm raise at `creator.length == 32`,
the first assertion after `initialized == 1` on an initialized distributor.
A signature for consent -> clvm raise in authorize_by_spend (`auth ==
AUTH_MESSAGE`), the only check that differs from the accepted control."""

from common import ASSETS, LEAD, POOL, BySignature, attempt, check, final_root, finish, settle, src, world

PARAMS = [10000, 0, 0, None, None, 1]


def run(tag, asset):
    print(f"\n{tag}")
    w, c = world(asset)
    # Alice funds and the router signs, but the campaign names Mallory as creator; Alice's coin sends
    # the creator's part (the old blank-cheque shape).
    attempt("ATTACK Alice's coin funds a campaign recorded with creator = mallory", w.create_raw, "mallory",
            c.amount, c.start, c.end, 1, POOL, PARAMS, 0, c.claim_window, consent="alice", expect="refuse",
            reason="MessageNotSentOrReceived")
    attempt("ATTACK the router alone (it also sends the creator's part), deposit supplied",
            w.create_raw, "mallory", c.amount, c.start, c.end, 1, POOL, PARAMS, 0, c.claim_window, consent="router",
            expect="refuse", reason="MessageNotSentOrReceived")
    wallet = w.bls()
    call = w.d.create(w.h, wallet.puzzle_hash, 1000, c.start, c.end, 1, POOL, PARAMS, 0, c.claim_window,
                      auth=BySignature(wallet.pk.to_bytes()))
    attempt("ATTACK creator consent by signature only (no coin spend)",
            lambda: w.push([call], [lambda: w.fee_spend(1000)]), expect="refuse", reason="clvm raise")
    for n in (0, 31, 33):
        attempt(f"ATTACK {n}-byte creator", w.create_raw, "creator", c.amount, c.start, c.end, 1, POOL, PARAMS, n,
                c.claim_window, creator_ph=b"\x11" * n, expect="refuse", reason="clvm raise")
    attempt("control: 32-byte creator, its own coin spend", w.create, c, "creator")
    check("recorded creator is the one that consented",
          w.state().campaign(c.id).creator == w.keys.ph("creator"))
    r = final_root(w, c, src((w.keys.ph("bob"), 1)))
    w.post(r)
    settle(w)
    w.advance_to(c.end + c.claim_window)
    attempt("control: the creator reclaims (bob never claimed)", w.reclaim, c.id)

    # The same length checks on the other leaves (review: "not probed").
    w, c = world(asset)
    for n in (31, 33):
        attempt(f"rotate to a {n}-byte key", lambda: w.push([w.d.rotate(w.h, b"\x22" * n)]),
                expect="refuse", reason="clvm raise")
        call = w.d.delegate(w.h, b"\x11" * n, w.keys.ph("hot"), 1)
        call.messages = []
        attempt(f"delegate with a {n}-byte owner", w.push, [call], expect="refuse", reason="clvm raise")


for tag, asset in ASSETS:
    run(tag, asset)
finish("FLPCR-07")
