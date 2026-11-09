"""FLPCR-03 rerun: a post naming a lagged `h` (old probe D/d1_window.py,
F/probes.py 3a/3b). The old build counted the window from `h`, which a
solution may understate by up to 31 blocks: D=38 gave 7 blocks, D=8 gave 1.

The fix (post_root.rue): activates_at = h + HEIGHT_WINDOW + dispute_blocks.
Since the inclusion height P <= h + 31, no root is claimable before
P + dispute_blocks. Covered by tests/test_chain.py::TestDisputeWindow (both
assets). This script measures the first height at which a claim lands, honest
and lagged, at both the testnet (8) and production (38) dispute windows. Every
earlier claim is refused by claim.rue's `assert !(state.active is nil)`: the
prologue has not promoted the pending root yet, and that is the claim leaf's
first assertion after the 32-byte check (which the holder passes).

Also the informational late veto (D-6): the guardian can still veto a root
after its activates_at as long as nobody has spent the singleton since,
because promotion is lazy and a veto's `h` may trail by 31 blocks. Recorded
as still present (documented behaviour, not a fund risk)."""

from common import ASSETS, HEIGHT_WINDOW, attempt, check, final_root, finish, src, world


def first_claim(asset, dispute_blocks, lag):
    w, c = world(asset, dispute_blocks=dispute_blocks, rotate_delay=max(24, dispute_blocks + 1))
    w.create(c, "creator")
    r = final_root(w, c, src((w.keys.ph("alice"), 1)))
    w.advance_to(w.h + HEIGHT_WINDOW)
    P = w.h  # the height the post is included at (ASSERT_BEFORE_HEIGHT h + 32 => P <= h + 31)
    w.post(r, h=P - lag)
    while True:
        try:
            paid = w.claim(r, c.id, "alice")
            return w.h - P, paid
        except ValueError as e:
            assert "clvm raise" in str(e), e
            w.sim.create_block()


def late_veto(asset):
    w, c = world(asset)
    w.create(c, "creator")
    r = final_root(w, c, src((w.keys.ph("alice"), 1)))
    w.post(r)
    activates_at = w.state().pending.activates_at
    w.advance_to(activates_at + 10)  # active by height, but nobody has spent the singleton since
    late = w.h - activates_at
    w.push([w.d.veto(w.h - (HEIGHT_WINDOW - 1), r.root_id)])  # h trails by 31: the prologue does not promote
    return late


for tag, asset in ASSETS:
    print(f"\n{tag}")
    for D in (8, 38):
        honest, _ = first_claim(asset, D, 0)
        lagged, paid = first_claim(asset, D, HEIGHT_WINDOW - 1)
        check(f"D={D} honest h=P: first claim at P+{honest}", honest >= D)
        check(f"D={D} ATTACK h=P-31: first claim at P+{lagged} (old build: P+{D - 31 if D > 31 else 1})",
              lagged >= D, f"(paid {paid}; must be >= P+{D})")
    attempt("info D-6 (still present, documented): guardian vetoes 10 blocks after activates_at with h lagged 31",
            late_veto, asset)
finish("FLPCR-03")
