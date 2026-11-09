"""FLPCR-12 rerun: anyone spends the singleton for free through `delegate`
and invalidates every prebuilt bundle (old probe D/d4_race.py).

Fix (delegate.rue): every grant or revoke emits ReserveFee delegate_fee
(testnet 1,000,000 mojos, production 50,000,000), so each spam spend costs
that much. Claims also name no generation now (mode 16 / AGG_SIG_UNSAFE), so
a holder's signed claim survives someone else's spend. Covered by
tests/test_chain.py::TestDelegateFee and TestOnlyTheHolderClaims::
test_a_claim_names_no_generation.

What is NOT fixed (and cannot be on the simulator): the race itself. A paid
delegate spend still lands first and a prebuilt operator bundle still fails
(DoubleSpend: it spends the old singleton coin); the operator rebuilds. The
mempool race is a coverage gap, as in the review."""

from common import ASSETS, attempt, final_root, finish, settle, src, world


def run(tag, asset):
    print(f"\n{tag}")
    w, c = world(asset, delegate_fee=1_000_000)
    w.create(c, "creator")
    spammer = w.bls()
    call = w.d.delegate(w.h, spammer.puzzle_hash, w.keys.ph("x"), 1)
    call.fee = 0  # the old free spam spend
    attempt("ATTACK free delegate spend (no fee in the bundle)", lambda: w.push([call]),
            expect="refuse", reason="ReserveFee")
    call = w.d.delegate(w.h, w.keys.ph("spammer"), w.keys.ph("x"), 1)
    call.fee = 999_999
    attempt("ATTACK delegate spend one mojo short of delegate_fee", lambda: w.push([call]),
            expect="refuse", reason="ReserveFee")

    r = final_root(w, c, src((w.keys.ph("alice"), 1), (w.keys.ph("bob"), 1)))
    w.post(r)
    settle(w)
    # Alice sends her claim message now; a prebuilt singleton bundle (bob's claim stands in for the
    # operator's post) is built against this generation.
    alice_before = w.balance("alice")  # before the 1-mojo coin that sends alice's message exists
    alice_call = w.claim_call(r, c.id, w.keys.ph("alice"))
    alice_msgs = [s for m in alice_call.messages for s in w.send(m.sender, m.message, m.mode)]
    prebuilt = w.d.prepare([w.claim_call(r, c.id, w.keys.ph("bob"))])
    prebuilt_spends = list(prebuilt.spends) + [s for m in prebuilt.messages for s in w.send(m.sender, m.message, m.mode)]
    attempt("the spammer's paid grant lands first (delegate_fee paid)", w.set_delegate, "spammer", w.keys.ph("x"))
    attempt("prebuilt bundle against the old generation (still loses the race)",
            w.sim.spend_coins, prebuilt_spends, [], expect="refuse", reason="DoubleSpend")
    attempt("rebuilt against the new generation: accepted", w.claim, r, c.id, "bob")

    def presigned_alice():
        prepared = w.d.prepare([w.claim_call(r, c.id, w.keys.ph("alice"))])
        w.sim.spend_coins(list(prepared.spends) + alice_msgs, [])
        w.d.apply(prepared)
        return w.balance("alice") - alice_before

    attempt("FIX: alice's claim message, sent before two other spends, still lands (generation-free)",
            presigned_alice)


for tag, asset in ASSETS:
    run(tag, asset)
finish("FLPCR-12")
