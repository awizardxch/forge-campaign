"""FLPCR-06 rerun: the guardian rotates itself in as operator and posts a
root paying itself, in one spend (old probe A/p1_guardian_posts.py).

The fix (rotate.rue, shared.rue prologue): a rotation is stored as pending and
adopted only from h + HEIGHT_WINDOW + rotate_delay; the old operator posts
until then; the admin (dev multisig) can cancel. Covered by
tests/test_chain.py::TestRotation (both assets).

Reason pinned: in the attack, post_root's message is required from the
CURRENT operator (engine_at(h) is still the old key); the guardian's "p"
message has no receiver and the operator's is never sent:
MessageNotSentOrReceived."""

from common import ASSETS, HEIGHT_WINDOW, attempt, check, final_root, finish, settle, src, world


def run(tag, asset):
    print(f"\n{tag}")
    w, c = world(asset)
    w.create(c, "creator")
    alice = w.keys.ph("alice")
    r = final_root(w, c, src((alice, 1)))
    guardian = w.cfg.guardian_ph
    from flpcr.engine import Root
    from flpcr.merkle import build_tree, claim_leaf
    # The guardian's root pays the whole budget to the guardian's own address.
    leaf = claim_leaf(w.launcher_id, c.id, guardian, c.budget)
    root, _ = build_tree([leaf])
    totals = [(c.id, c.budget)]

    attempt("negative control: guardian post_root without rotate",
            lambda: w.push([w.d.post_root(w.h, 1, r.start, r.end, root, totals, sender=guardian)]),
            expect="refuse", reason="MessageNotSentOrReceived")
    attempt("ATTACK guardian rotate(self) + post_root(self-paying) in one spend",
            lambda: w.push([w.d.rotate(w.h, guardian),
                            w.d.post_root(w.h, 1, r.start, r.end, root, totals, sender=guardian)]),
            expect="refuse", reason="MessageNotSentOrReceived")
    attempt("ATTACK guardian rotate(self) now, post next block", lambda: (
        w.rotate("guardian"),
        w.push([w.d.post_root(w.h, 1, r.start, r.end, root, totals, sender=guardian)]))[1],
        expect="refuse", reason="MessageNotSentOrReceived")
    check("the rotation is public and pending", w.state().rotation is not None
          and w.state().rotation.engine_ph == guardian,
          f"(effective at {w.state().rotation.effective_at}, now {w.h})")
    attempt("guardian cannot cancel its own rotation (admin only)", w.cancel_rotation, "guardian", sender="guardian",
            expect="refuse", reason="MessageNotSentOrReceived")
    attempt("FIX: admin cancels the pending rotation", w.cancel_rotation, "guardian")
    w.advance_to(w.h + HEIGHT_WINDOW + w.cfg.rotate_delay)
    attempt("after the delay, the cancelled guardian still cannot post",
            lambda: w.push([w.d.post_root(w.h, 1, r.start, r.end, root, totals, sender=guardian)]),
            expect="refuse", reason="MessageNotSentOrReceived")
    w.post(r)
    settle(w)
    attempt("control: operator posts, alice claims the honest root", w.claim, r, c.id, "alice")


for tag, asset in ASSETS:
    run(tag, asset)
finish("FLPCR-06")
