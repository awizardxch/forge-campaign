"""FLPCR-15 rerun: a campaign starting up to 31 blocks in the past (old probe
D/d2_retro.py: with h = P - 31, start = P - 31 was accepted and 277,143 of
970,000 never reached holders).

Fix (create.rue): assert start >= h + HEIGHT_WINDOW. Since the inclusion
height P <= h + 31, every campaign starts after the block that creates it.
Covered by tests/test_chain.py::TestStartAndWindow::test_start_lead.

Reason: clvm raise at `start >= h + HEIGHT_WINDOW`. On an initialized
distributor with a 32-byte creator, amount 1,000 (budget > 0), it is the
first create assertion these solutions can fail: they differ from the
accepted control only in `start` (and `h`)."""

from common import ASSETS, HEIGHT_WINDOW, POOL, attempt, check, finish, world

PARAMS = [10000, 0, 0, None, None, 1]


def run(tag, asset):
    print(f"\n{tag}")
    w, _ = world(asset)
    w.advance_to(w.h + HEIGHT_WINDOW + 5)
    P = w.h
    lag = P - (HEIGHT_WINDOW - 1)

    def create(start, h, nonce):
        return w.create_raw("creator", 1000, start, start + 40, 1, POOL, PARAMS, nonce, 56, h=h)

    attempt("ATTACK h = P - 31, start = h (old: accepted, 31 blocks retroactive)", create, lag, lag, 0,
            expect="refuse", reason="clvm raise")
    attempt("ATTACK h = P - 31, start = P (h + 31)", create, P, lag, 1, expect="refuse", reason="clvm raise")
    attempt("ATTACK h = P, start = P + 31", create, P + 31, P, 2, expect="refuse", reason="clvm raise")
    attempt("control: h = P - 31, start = h + 32 = P + 1", create, lag + HEIGHT_WINDOW, lag, 3)
    newest = w.state().campaigns[0]
    check("the accepted campaign starts after the block that created it", newest.start > P,
          f"(start {newest.start}, created at {P})")
    attempt("control: h = P, start = P + 32", create, P + HEIGHT_WINDOW, P, 4)


for tag, asset in ASSETS:
    run(tag, asset)
finish("FLPCR-15")
