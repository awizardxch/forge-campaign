"""A-1: the guardian alone (no operator key) posts a root, via rotate + post_root in one spend,
and the guardian's own address then claims the whole budget. SPEC §7: guardian 'Cannot: Post roots;
move funds'."""
from common import *

for asset in (bytes(32), b"\xa5" * 32):
    print(f"--- reward asset {'XCH' if asset == bytes(32) else 'CAT'}")
    # Honest control: the operator posts the engine's root; the holder claims.
    w, c = world(asset)
    w.create(c, "creator")
    alice = w.keys.ph("alice")
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, alice, 1)]})
    w.advance_to(c.end + 1)
    _, r = engine_root(w, c, src, c.end)
    attempt("control: operator posts engine root", w.post, r)
    w.advance_to(w.h + w.cfg.dispute_blocks)
    attempt("control: alice claims", w.claim, r, c.id, "alice")

    # Negative control: a guardian message cannot post directly (engine_ph != guardian_ph).
    w, c = world(asset)
    w.create(c, "creator")
    w.advance_to(c.end + 1)
    g = w.keys.ph("guardian")
    bad = make_root(w, 1, c.start, c.end, [(c.id, g, 970_000)])
    attempt("control: guardian post_root without rotate", w.post, bad, sender="guardian", expect="refuse")

    # Attack: one singleton spend, two actions, only the guardian's coins send messages.
    w.d.state  # unchanged after refusal
    args = [1, c.start, c.end, bad.root, sorted(bad.totals.items()), bytes(32)]
    actions = [Action("rotate", [w.h, g]), Action("post_root", [w.h, *args])]
    builders = [lambda: w.message_from("guardian", b"r" + g),
                lambda: w.message_from("guardian", b"p" + sha256tree(args))]
    attempt("ATTACK: guardian rotate(self)+post_root in one spend", w.push, actions, builders)
    print("    engine_ph is now guardian:", w.d.read_state()["engine_ph"] == g)
    w.advance_to(w.h + w.cfg.dispute_blocks)
    before = w.balance("guardian")
    attempt("ATTACK: guardian's address claims the whole budget", w.claim, bad, c.id, "guardian")
    print("    guardian gained", w.balance("guardian") - before, "of budget 970000; alice (the real holder) got 0")
