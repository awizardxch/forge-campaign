"""A-2: `create` takes `creator` from the solution, authorized only by the router's "c"+id.
(a) alice's coin funds a campaign whose creator is mallory; mallory reclaims it.
(b) a creator of the wrong width (31, 33, 0 bytes) is accepted; the leftover can then never be reclaimed."""
from common import *


def create_with_creator(w, c, creator_bytes, funder="alice"):
    sol = [w.h, creator_bytes, c.amount, c.start, c.end, int(c.kind), c.target, c.params.as_clvm(), c.nonce,
           c.claim_window]
    cid = sha256tree(["flpcr-campaign-v1", w.launcher_id, creator_bytes, w.cfg.reward_asset, c.amount, c.start,
                      c.end, int(c.kind), c.target, sha256tree(c.params.as_clvm()), c.nonce, w.cfg.fee_bps,
                      c.claim_window])

    def deposit():
        coin = w.sim.new_coin(w.keys.ph(funder), c.amount)
        return [w.keys.spend(coin, funder, [])]

    w.push([Action("create", sol)], [lambda: w.message_from("router", b"c" + cid), deposit])
    return cid


# (a) creator substitution
w, c = world()
cid = create_with_creator(w, c, w.keys.ph("mallory"), funder="alice")
print("created; creator recorded =", "mallory" if w.d.read_state()["campaigns"][cid]["creator"] == w.keys.ph("mallory") else "?")
w.advance_to(c.end + WINDOW)
attempt("ATTACK(a): mallory reclaims alice's deposit", w.reclaim, cid)
attempt("control(a): alice cannot reclaim", lambda: None)  # nothing left; shown by state below
print("    campaigns left:", len(w.d.read_state()["campaigns"]), "mallory balance:", w.balance("mallory"))

# (b) wrong-width creator
for width in (31, 33, 0):
    w, c = world()
    cr = b"\x11" * width
    try:
        cid = create_with_creator(w, c, cr)
        print(f"[UNEXPECTED?] create with {width}-byte creator: ACCEPTED, reserve={w.d.read_state()['reserve']}")
    except Exception as e:
        print(f"create with {width}-byte creator: REFUSED {str(e)[:120]}")
        continue
    w.advance_to(c.end + WINDOW)
    # every reclaim form: message from a coin at the creator's ph (cannot exist), signature, delegate
    sol_msg = [w.h, cid, AUTH_MESSAGE, None]
    attempt(f"  reclaim {width}-byte creator by message", w.push, [Action("reclaim", sol_msg)], expect="refuse")
    k = w.sim.bls(1)
    sol_sig = [w.h, cid, AUTH_SIGNATURE, k.pk.to_bytes()]
    attempt(f"  reclaim {width}-byte creator by signature", w.push, [Action("reclaim", sol_sig)], secret_keys=[k.sk],
            expect="refuse")
    # delegation: the 'creator' would have to grant, which needs authorize(creator...) -> impossible
    sol_del = [w.h, cr, k.puzzle_hash, 1, AUTH_MESSAGE, None]
    attempt(f"  grant a delegate for {width}-byte creator", w.push, [Action("delegate", sol_del)], expect="refuse")
    print("    reserve still holds", w.d.read_state()["reserve"])

# control: well-formed creator reclaims
w, c = world()
w.create(c, "creator")
w.advance_to(c.end + WINDOW)
attempt("control(b): 32-byte creator reclaims", w.reclaim, c.id)
