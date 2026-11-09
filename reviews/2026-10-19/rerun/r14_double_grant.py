"""FLPCR-14 rerun: grant twice, revoke once, the delegate stays active (old
probes B/probe_slots.py delegation_probes(), A/p3_double_grant.py), plus the
old safe case A/p5_revoke_same_spend.py on the new list.

Fix (shared.rue, delegate.rue): delegation slots are a sorted unique-key list
keyed by tree_hash((owner . delegate)) with (active, uses); a second grant
updates the one slot, a revoke sets active = 0, and a delegated use spends
(key, 1, uses). Covered by tests/test_chain.py::TestDelegatedClaims::
test_grant_twice_revoke_once_revokes, test_a_second_slot_for_a_granted_key_is_refused,
test_revoking_a_never_granted_pair_is_refused, TestSpentSlotsStaySpent.

Reasons: a revoked delegate's claim spends (key, 1, uses), which no coin is
(the live slot is (key, 0, uses)): MessageNotSentOrReceived. A re-insert of a
live key between other neighbours: clvm raise at the ordering asserts (left
key < key < right key), the first check the forged neighbours can fail."""

from common import ASSETS, BySignature, Distributor, attempt, check, final_root, finish, settle, src, world


def run(tag, asset):
    print(f"\n{tag}")
    w, c = world(asset)
    cold, hot = w.bls(), w.bls()
    w.create(c, "creator")
    r = final_root(w, c, src((cold.puzzle_hash, 1), (w.keys.ph("bob"), 1)))
    w.post(r)
    settle(w)
    key = Distributor.delegation_key(cold.puzzle_hash, hot.puzzle_hash)

    w.set_delegate(cold, hot.puzzle_hash)
    w.set_delegate(cold, hot.puzzle_hash, grant=0)
    attempt("control (grant, revoke): delegated claim", w.claim_delegated, r, c.id, cold.puzzle_hash, hot,
            expect="refuse", reason="MessageNotSentOrReceived")
    w.set_delegate(cold, hot.puzzle_hash)
    w.set_delegate(cold, hot.puzzle_hash)
    w.set_delegate(cold, hot.puzzle_hash, grant=0)
    attempt("ATTACK (grant, grant, revoke): delegated claim (was ACCEPTED, 485000)", w.claim_delegated, r, c.id,
            cold.puzzle_hash, hot, expect="refuse", reason="MessageNotSentOrReceived")
    rec = w.d.delegations[key]
    ph = w.d.delegation_slot_puzzle(key, rec).tree_hash()
    live = [x for x in w.sim.unspent_coins(ph, False)]
    check("one slot for the pair, inactive, uses counted", (rec.active, len(live)) == (0, 1),
          f"(active {rec.active}, uses {rec.uses}, unspent coins at its puzzle hash {len(live)})")

    # A second slot for the key, inserted between other neighbours, is refused by ordering.
    others = [w.bls() for _ in range(6)]
    for o in others:
        w.set_delegate(cold, o.puzzle_hash)
    keys = sorted(w.d.delegations)
    lk, rk = next((a, b) for a, b in zip(keys, keys[1:]) if not (a <= key <= b))
    L, R = w.d.delegations[lk], w.d.delegations[rk]
    call = w.d.delegate(w.h, cold.puzzle_hash, hot.puzzle_hash, 1, BySignature(cold.pk.to_bytes()))
    call.action.solution[4] = 1
    call.action.solution[5] = [lk, L.active, L.uses, L.left, rk, R.active, R.uses, R.right]
    call.delegation_updates = {}
    attempt("ATTACK insert a second slot for the existing key elsewhere in the list", w.push, [call],
            expect="refuse", reason="clvm raise")

    w.set_delegate(cold, hot.puzzle_hash)
    # p5 shape: the holder revokes and the delegate claims in the same spend.
    calls = lambda: [w.d.delegate(w.h, cold.puzzle_hash, hot.puzzle_hash, 0, BySignature(cold.pk.to_bytes())),  # noqa
                     w.claim_call(r, c.id, cold.puzzle_hash, auth=__import__("common").ByDelegate(
                         hot.puzzle_hash, BySignature(hot.pk.to_bytes())))]
    attempt("revoke + delegated claim in one spend: driver", w.d.prepare, calls(), expect="refuse",
            reason="delegation")
    saved = w.d._check_collisions
    w.d._check_collisions = lambda *a: None
    attempt("  ... driver check off: consensus (both spend the one live slot)", w.push, calls(), expect="refuse")
    w.d._check_collisions = saved
    attempt("control: re-granted, the delegate claims (paid to holder)", w.claim_delegated, r, c.id,
            cold.puzzle_hash, hot, signed=True)


for tag, asset in ASSETS:
    run(tag, asset)
finish("FLPCR-14")
