"""Reviewer B: slot / sorted-list / delegation-slot probes against the compiled FLPCR puzzles.

Run: PYTHONPATH=<repo> python probe_slots.py
Every adversarial probe has an honest control in the same world first.
"""
import sys, traceback
sys.path.insert(0, __import__('pathlib').Path(__file__).resolve().parents[4].as_posix())
sys.path.insert(0, __import__('pathlib').Path(__file__).resolve().parents[4].as_posix() + "/tests")

from chia_wallet_sdk import Coin, CoinSpend, Constants
from flpcr import EngineState, FixtureSource, HolderEvent, Kind, run_epoch
from flpcr.clvm import sha256tree
from flpcr.chain.sim import SimWorld
from flpcr.chain.driver import AUTH_MESSAGE, AUTH_SIGNATURE, AUTH_DELEGATE, Action, Distributor, LOW, HIGH
from flpcr.chain.values import to_program

POOL, LP = b"\xb0" * 32, b"\x1b" * 32
WINDOW = 16
RESULTS = []


def outcome(fn, *a, **k):
    try:
        r = fn(*a, **k)
        return "ACCEPTED", r
    except ValueError as e:
        m = str(e)
        if "clvm raise" in m or "Simulator error" in m:
            return "REFUSED: " + m.splitlines()[0][:160], None
        raise


def record(name, expect, got):
    ok = got.replace("Eval error: ","").startswith(expect)
    RESULTS.append((name, expect, got, ok))
    print(f"[{'OK ' if ok else 'BAD'}] {name}: expected {expect!r} got {got!r}")


def world(holders, extra_events=()):
    w = SimWorld()
    w.advance_to(w.h + 3)
    camp = w.campaign("creator", 1_000_000, w.h + 2, w.h + 42, Kind.LP_HOLD, POOL, claim_window=WINDOW)
    events = [HolderEvent(0, ph, amt) for ph, amt in holders] + list(extra_events)
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: events})
    w.create(camp, "creator")
    w.advance_to(camp.end + 1)
    _, r = run_epoch(EngineState(w.launcher_id, height=camp.start), [camp], src, camp.end)
    w.post(r)
    w.advance_to(w.h + w.cfg.dispute_blocks)
    return w, camp, r


def push_claim_raw(w, r, cid, ph, label, insert, info):
    cl = r.claim_for(cid, ph)
    sol = [w.h, cid, ph, cl.cumulative, cl.proof, insert, info, AUTH_MESSAGE, None]
    return w.push([Action("claim", sol)], [w.holder_message(label, cid, ph, cl.cumulative)])


# --------------------------------------------------------------------------------------------
def probe_order_and_duplicates():
    labels = [f"h{i}" for i in range(12)]
    w0 = SimWorld()
    phs = {l: w0.keys.ph(l) for l in labels}
    w, c, r = world([(w0.keys.ph(l), 1) for l in labels] + [(w0.keys.ph("whale"), 1000)])
    keys = {l: Distributor.claim_key(c.id, w.keys.ph(l)) for l in labels}
    hi = [l for l in labels if keys[l][0] >= 0x80]
    lo = [l for l in labels if keys[l][0] < 0x80]
    # Honest control: keys with the top bit set and clear both insert (proves `>s`, not signed `>`).
    got, _ = outcome(w.claim, r, c.id, hi[0]); record("control: insert key with top bit set (0x%02x..)" % keys[hi[0]][0], "ACCEPTED", got)
    got, _ = outcome(w.claim, r, c.id, lo[0]); record("control: insert key with top bit clear (0x%02x..)" % keys[lo[0]][0], "ACCEPTED", got)

    # Attack: duplicate insert of an existing key, left neighbour = the key itself (equal key)
    a = hi[0]; ka = keys[a]; d = w.d
    A = d.slots[ka]; R = d.slots[A.right]
    info = [ka, A.claimed, A.left, A.right, R.claimed, R.right]
    got, _ = outcome(push_claim_raw, w, r, c.id, w.keys.ph(a), a, 1, info)
    record("attack: re-insert existing key with itself as left neighbour", "REFUSED: clvm raise", got)

    # Attack: stale neighbours (the pair that surrounded the key before it was inserted)
    L = d.slots[A.left]
    stale = [A.left, L.claimed, L.left, A.right, R.claimed, R.right]  # L.right is now ka, so this L value is spent
    got, _ = outcome(push_claim_raw, w, r, c.id, w.keys.ph(a), a, 1, stale)
    record("attack: re-insert existing key between its former (stale) neighbours", "REFUSED: Simulator error", got)

    # Attack: update mode with fabricated lower claimed (0) on the real left/right
    cl = r.claim_for(c.id, w.keys.ph(a))
    sol = [w.h, c.id, w.keys.ph(a), cl.cumulative, cl.proof, 0, (0, (A.left, A.right)), AUTH_MESSAGE, None]
    got, _ = outcome(w.push, [Action("claim", sol)], [w.holder_message(a, c.id, w.keys.ph(a), cl.cumulative)])
    record("attack: update mode with fabricated claimed=0", "REFUSED: Simulator error", got)

    # Attack: two inserts of one new key in one spend, same neighbours
    b = lo[1]; kb = keys[b]
    lk, rk = d.neighbours(kb); Ls, Rs = d.slots[lk], d.slots[rk]
    info = [lk, Ls.claimed, Ls.left, rk, Rs.claimed, Rs.right]
    clb = r.claim_for(c.id, w.keys.ph(b))
    sol = [w.h, c.id, w.keys.ph(b), clb.cumulative, clb.proof, 1, info, AUTH_MESSAGE, None]
    msg = w.holder_message(b, c.id, w.keys.ph(b), clb.cumulative)
    got, _ = outcome(w.push, [Action("claim", list(sol)), Action("claim", list(sol))], [msg, msg])
    record("attack: same key inserted twice in one spend", "REFUSED", got)

    # Honest control: the same single insert solution, alone, is accepted.
    got, _ = outcome(w.push, [Action("claim", list(sol))], [msg], slot_updates={lk: (Ls.claimed, Ls.left, kb), rk: (Rs.claimed, kb, Rs.right), kb: (clb.cumulative, lk, rk)})
    record("control: that insert alone", "ACCEPTED", got)
    # Honest two-action control: two different keys in one spend (driver threads ephemeral neighbours? no:
    # use test_several_actions_in_one_spend in the repo suite for that).
    return w, c, r, keys, labels


# --------------------------------------------------------------------------------------------
def forged_slot_probes():
    """Forged claim slot (reset claimed to 0) at the correct slot puzzle hash."""
    w0 = SimWorld()
    w, c, r = world([(w0.keys.ph("alice"), 1), (w0.keys.ph("whale"), 1000)])
    alice = w.keys.ph("alice")
    got, paid = outcome(w.claim, r, c.id, "alice"); record("control: alice claims once", "ACCEPTED", got)
    got, _ = outcome(w.claim, r, c.id, "alice"); record("control: alice second claim refused", "REFUSED", got)
    d = w.d
    key = Distributor.claim_key(c.id, alice)
    A = d.slots[key]
    fake_value = (key, (0, (A.left, A.right)))  # claimed reset to 0
    fake_puzzle = d.slot_puzzle(fake_value)
    fake_ph = fake_puzzle.tree_hash()

    # (a) Realistic forge: attacker's own coin creates a 0-mojo coin at the fake slot ph.
    att = w.sim.new_coin(w.keys.ph("attacker"), 1)
    w.sim.spend_coins([w.keys.spend(att, "attacker", [[51, fake_ph, 0], [51, w.keys.ph("attacker"), 1]])], [])
    forged = Coin(att.coin_id(), fake_ph, 0)
    assert w.sim.coin_state(forged.coin_id()) is not None
    cl = r.claim_for(c.id, alice)
    sol = [w.h, c.id, alice, cl.cumulative, cl.proof, 0, (0, (A.left, A.right)), AUTH_MESSAGE, None]
    spends, new_state, _ = d.spend([Action("claim", sol)])
    inner_ph = d.inner_puzzle(d.state).tree_hash()
    for parent_proof in [(att.parent_coin_info, (w.keys.ph("attacker"), 1)),
                         (A.creator_parent, (A.creator_inner_ph, 1))]:
        slot_sol = to_program(w.clvm, (parent_proof, inner_ph))
        bundle = spends + [CoinSpend(forged, fake_puzzle.serialize(), slot_sol.serialize())]
        bundle += w.holder_message("alice", c.id, alice, cl.cumulative)()
        got, _ = outcome(w.sim.spend_coins, bundle, [])
        record("attack: forged claim slot (claimed=0) from a non-singleton parent", "REFUSED: Simulator error", got)

    # (b) Attacker coin at a singleton puzzle hash of THIS launcher, with an attacker inner puzzle that
    # would create the forged slot. It must be unspendable (lineage), else (a) would be reachable.
    evil_inner = w.clvm.parse("(q (51 0x%s ()) (51 0x%s 1))" % (fake_ph.hex(), w.keys.ph("attacker").hex()))
    evil_full = d.p.singleton_mod.curry([d.singleton_struct, evil_inner])
    att2 = w.sim.new_coin(w.keys.ph("attacker"), 1)
    w.sim.spend_coins([w.keys.spend(att2, "attacker", [[51, evil_full.tree_hash(), 1]])], [])
    evil_coin = Coin(att2.coin_id(), evil_full.tree_hash(), 1)
    for lineage in ([att2.parent_coin_info, w.keys.ph("attacker"), 1],     # claims a singleton parent
                    [att2.coin_id(), 1],                                     # eve form
                    [d.coin.parent_coin_info, d.lineage[1] if len(d.lineage) == 3 else None, 1]):
        lineage = [x for x in lineage if x is not None]
        sol2 = to_program(w.clvm, [lineage, 1, None])
        got, _ = outcome(w.sim.spend_coins, [CoinSpend(evil_coin, evil_full.serialize(), sol2.serialize())], [])
        record("attack: spend attacker coin at singleton_ph(launcher, evil_inner), lineage=%d-tuple" % len(lineage),
               "REFUSED", got)

    # (c) Sim-only: a coin whose parent id *looks* like a singleton generation (insert_coin skips lineage).
    # Shows the slot's only defence is that such a parent cannot exist on chain.
    fake_parent_info = b"\x11" * 32
    fake_parent = Coin(fake_parent_info, d.full_puzzle_hash_for_inner(b"\x22" * 32), 1)
    impossible = Coin(fake_parent.coin_id(), fake_ph, 0)
    w.sim.insert_coin(impossible)
    spends, new_state, _ = d.spend([Action("claim", sol)])
    slot_sol = to_program(w.clvm, ((fake_parent_info, (b"\x22" * 32, 1)), inner_ph))
    bundle = spends + [CoinSpend(impossible, fake_puzzle.serialize(), slot_sol.serialize())]
    bundle += w.holder_message("alice", c.id, alice, cl.cumulative)()
    got, _ = outcome(w.sim.spend_coins, bundle, [])
    record("sim-only: insert_coin slot with fabricated singleton-looking parent (double claim)", "ACCEPTED", got)


# --------------------------------------------------------------------------------------------
def delegation_probes():
    for dup in (False, True):
        w0 = SimWorld()
        w = SimWorld()
        w.advance_to(w.h + 3)
        cold, hot = w.sim.bls(1), w.sim.bls(1)
        camp = w.campaign("creator", 1_000_000, w.h + 2, w.h + 42, Kind.LP_HOLD, POOL, claim_window=WINDOW)
        src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, cold.puzzle_hash, 1),
                                                               HolderEvent(0, w.keys.ph("bob"), 1)]})
        w.create(camp, "creator")
        w.set_delegate(cold, hot.puzzle_hash)
        first = w.d.delegations[(cold.puzzle_hash, hot.puzzle_hash)]
        if dup:
            got, _ = outcome(w.set_delegate, cold, hot.puzzle_hash)
            record("dup grant: second grant of the same (holder, delegate) accepted", "ACCEPTED", got)
        got, _ = outcome(w.set_delegate, cold, hot.puzzle_hash, grant=0)
        record(("dup" if dup else "control") + ": holder revokes once", "ACCEPTED", got)
        w.advance_to(camp.end + 1)
        _, r = run_epoch(EngineState(w.launcher_id, height=camp.start), [camp], src, camp.end)
        w.post(r)
        w.advance_to(w.h + w.cfg.dispute_blocks)
        if dup:
            # Forged delegation slot for a thief from a non-singleton parent
            thief = w.sim.bls(1)
            ph = w.d.delegation_puzzle(cold.puzzle_hash, thief.puzzle_hash)
            att = w.sim.new_coin(w.keys.ph("attacker"), 1)
            w.sim.spend_coins([w.keys.spend(att, "attacker", [[51, ph.tree_hash(), 0], [51, w.keys.ph("attacker"), 1]])], [])
            w.d.delegations[(cold.puzzle_hash, thief.puzzle_hash)] = first  # any lineage; coin replaced below
            forged = Coin(att.coin_id(), ph.tree_hash(), 0)
            action, _u = w.claim_action(r, camp.id, cold.puzzle_hash, auth=AUTH_DELEGATE,
                                        auth_data=(thief.puzzle_hash, (AUTH_SIGNATURE, thief.pk.to_bytes())))
            spends, _ns, _pa = w.d.spend([action])
            fixed = [CoinSpend(forged, s_.puzzle_reveal, s_.solution) if s_.coin.puzzle_hash == ph.tree_hash() else s_
                     for s_ in spends]
            assert any(s_.coin.coin_id() == forged.coin_id() for s_ in fixed)
            got, _ = outcome(w.sim.spend_coins, fixed, [thief.sk])
            del w.d.delegations[(cold.puzzle_hash, thief.puzzle_hash)]
            record("attack: forged delegation slot (non-singleton parent)", "REFUSED: Simulator error", got)
        # Point the driver at the first grant's slot (the driver forgot it on the second grant).
        w.d.delegations[(cold.puzzle_hash, hot.puzzle_hash)] = first
        got, paid = outcome(w.claim_delegated, r, camp.id, cold.puzzle_hash, hot)
        if dup:
            record("dup: revoked delegate still claims (paid %s to holder)" % paid, "ACCEPTED", got)
        else:
            record("control: revoked delegate refused", "REFUSED", got)


if __name__ == "__main__":
    for p in (probe_order_and_duplicates, forged_slot_probes, delegation_probes):
        try:
            p()
        except Exception:
            traceback.print_exc()
            RESULTS.append((p.__name__, "no exception", "EXCEPTION", False))
    print("\nSUMMARY: %d/%d as expected" % (sum(r[3] for r in RESULTS), len(RESULTS)))
