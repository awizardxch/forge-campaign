"""Audit 2026-11-11 (B2) fixes, each shown with the finding's own attack shape.

CE-13 (High): a valid spend whose leaf selector is a program, `(q . leaf)`, is accepted on
chain. The reader took every selector for a path atom and raised, so rebuild-from-chain failed
for good. Fixed in the reader: a selector is RUN on the revealed leaves, as the action layer runs
it (`resync._select`).

CP-06 (Medium): slot-machine's reserve finalizer takes the reserve's parent from the solution, so
any coin at the reserve's puzzle hash holding the state's amount can stand in for the reserve
(free while the reserve is 0). Fixed twice:
  * in the puzzle, for every new distributor: `pinned_reserve_finalizer.rue` curries the parent
    and re-curries it every generation, so the decoy spend is refused by consensus;
  * in the reader, for every distributor launched before it (upstream finalizer): resync follows
    the reserve each spend actually messaged instead of refusing to boot.

Each test fails against the code before its fix (the CP-06 puzzle test is also pinned by a
mutation of the finalizer: see PROOF.md).
"""

import unittest

try:
    from chia_wallet_sdk import Clvm, Coin, CoinSpend

    from flpcr.chain.driver import FINALIZER_PINNED, FINALIZER_UPSTREAM, HEIGHT_WINDOW, Puzzles
    from flpcr.chain.resync import resync
    from flpcr.chain.sim import SimWorld
    from flpcr.chain.values import to_int, to_list

    HAVE_CHAIN = True
except (ImportError, FileNotFoundError):  # pragma: no cover
    HAVE_CHAIN = False

from flpcr import EngineState, FixtureSource, HolderEvent, Kind, run_epoch

from test_chain import refused

POOL, LP = b"\xb0" * 32, b"\x1b" * 32


def funded_world(finalizer=None, reward_asset=bytes(32)):
    """A distributor with one campaign and an active root; alice (3) and bob (1) hold LP."""
    w = SimWorld(delegate_fee=0, finalizer=finalizer, reward_asset=reward_asset)
    w.advance_to(w.h + 3)
    alice, bob = w.keys.ph("alice"), w.keys.ph("bob")
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, alice, 3), HolderEvent(0, bob, 1)]})
    start = w.h + HEIGHT_WINDOW + 2
    c = w.campaign("creator", 1_000_000, start, start + 40, Kind.LP_HOLD, POOL, claim_window=160)
    w.create(c, "creator")
    end = start + 20
    w.advance_to(end + 1)
    _, r = run_epoch(EngineState(w.launcher_id, height=start), [c], src, end)
    w.post(r)
    w.advance_to(w.h + HEIGHT_WINDOW + w.cfg.dispute_blocks)
    return w, c, r


def rewrite_singleton_solution(w, rewrite):
    """Every singleton spend the driver builds until `undo()` has its action-layer solution
    passed through `rewrite(clvm, inner_solution)`. Everything else is the honest driver's."""
    original = w.d._action_layer_spend

    def patched(actions):
        spend = original(actions)
        c = w.clvm
        sol = to_list(c.deserialize(spend.solution))
        return CoinSpend(spend.coin, spend.puzzle_reveal, c.list([sol[0], sol[1], rewrite(c, sol[2])]).serialize())

    w.d._action_layer_spend = patched
    return lambda: setattr(w.d, "_action_layer_spend", original)


def quote_selectors(clvm, inner):
    """Each selector, a path, rewritten as `(q . leaf)`: the leaf it picks, quoted."""
    leaves = inner.first()
    out = []
    for entry in to_list(inner.rest().first()):
        path, node = to_int(entry.first()), leaves
        while path > 1:
            node = node.rest() if path & 1 else node.first()
            path >>= 1
        out.append(clvm.pair(clvm.pair(clvm.atom(b"\x01"), node), entry.rest()))
    return clvm.pair(leaves, clvm.pair(clvm.list(out), inner.rest().rest()))


def plant_decoy(w, amount):
    """Mallory's own coin creates a coin at the reserve's (XCH) puzzle hash holding `amount`."""
    src = w.sim.new_coin(w.keys.ph("mallory"), amount + 1)
    w.sim.spend_coins([w.keys.spend(src, "mallory", [[51, w.d.reserve_ph, amount], [51, w.keys.ph("change"), 1]])], [])
    return Coin(src.coin_id(), w.d.reserve_ph, amount)


def spend_decoy_as_reserve(w, decoy):
    """Mallory's builder: the honest bundle, with the decoy spent in the reserve's place and its
    parent written where upstream's finalizer reads the reserve's parent (the solution's trailing
    value), as the B2 probe did. The pinned finalizer ignores that value; writing it anyway is
    what makes a mutant that reads it again fail these tests."""
    real = w.d.reserve_coin
    original_reserve, original_layer = w.d._reserve_spends, w.d._action_layer_spend

    def reserve_spends(*a, **kw):
        out = original_reserve(*a, **kw)
        return [CoinSpend(decoy, s.puzzle_reveal, s.solution) if s.coin.coin_id() == real.coin_id() else s for s in out]

    def layer(actions):
        spend = original_layer(actions)
        c = w.clvm
        sol = to_list(c.deserialize(spend.solution))
        inner = sol[2]
        inner = c.pair(inner.first(), c.pair(inner.rest().first(), c.pair(inner.rest().rest().first(),
                                                                           c.atom(decoy.parent_coin_info))))
        return CoinSpend(spend.coin, spend.puzzle_reveal, c.list([sol[0], sol[1], inner]).serialize())

    w.d._reserve_spends, w.d._action_layer_spend = reserve_spends, layer

    def undo():
        w.d._reserve_spends, w.d._action_layer_spend = original_reserve, original_layer
    return undo


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestCE13ProgramSelector(unittest.TestCase):
    def check(self, finalizer):
        w, c, r = funded_world(finalizer)
        alice = w.keys.ph("alice")
        undo = rewrite_singleton_solution(w, quote_selectors)
        before = w.balance(alice)
        w.push([w.claim_call(r, c.id, alice)])  # the chain accepts it: the puzzle enforces no selector form
        undo()
        self.assertGreater(w.balance(alice) - before, 0)
        got = resync(w.sim, Puzzles(Clvm()), w.launcher_id)  # raised `not an atom` before the fix
        self.assertEqual(got.distributor.state.tree_hash(), w.d.state.tree_hash())
        self.assertEqual(got.distributor.coin.coin_id(), w.d.coin.coin_id())
        self.assertEqual(got.distributor.reserve_coin.coin_id(), w.d.reserve_coin.coin_id())
        self.assertEqual(got.distributor.claims.keys(), w.d.claims.keys())
        self.assertIn("claim", [a.leaf for a in got.actions])
        # and the rebuilt driver keeps working on chain: bob claims through it
        w.d = got.distributor
        self.assertGreater(w.claim(r, c.id, "bob"), 0)
        self.assertEqual(resync(w.sim, Puzzles(Clvm()), w.launcher_id).distributor.state.tree_hash(),
                         w.d.state.tree_hash())

    def test_resync_survives_a_quoted_selector(self):
        self.check(None)

    def test_resync_survives_a_quoted_selector_upstream_finalizer(self):
        self.check(FINALIZER_UPSTREAM)

    def test_a_selector_that_names_no_leaf_is_refused_by_the_chain(self):
        # (q . something-else) fails the merkle proof: the reader never sees it.
        w, c, r = funded_world()
        alice = w.keys.ph("alice")

        def bogus(clvm, inner):
            out = [clvm.pair(clvm.pair(clvm.atom(b"\x01"), clvm.atom(b"x")), e.rest())
                   for e in to_list(inner.rest().first())]
            return clvm.pair(inner.first(), clvm.pair(clvm.list(out), inner.rest().rest()))

        undo = rewrite_singleton_solution(w, bogus)
        try:
            # SimWorld.push runs prepare (local, honest) and then the chain refuses the rewritten spend
            refused(self, w.push, [w.claim_call(r, c.id, alice)])
        finally:
            undo()


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestCP06DecoyReserve(unittest.TestCase):
    def test_new_distributors_run_the_pinned_finalizer(self):
        w = SimWorld(delegate_fee=0)
        self.assertEqual(w.d.finalizer_kind, FINALIZER_PINNED)
        got = resync(w.sim, Puzzles(Clvm()), w.launcher_id)
        self.assertEqual(got.distributor.finalizer_kind, FINALIZER_PINNED)

    def test_pinned_a_free_decoy_at_reserve_0_is_refused(self):
        w = SimWorld(delegate_fee=0)
        w.advance_to(w.h + 3)
        real = w.d.reserve_coin
        self.assertEqual(real.amount, 0)
        decoy = plant_decoy(w, 0)
        undo = spend_decoy_as_reserve(w, decoy)
        try:
            refused(self, w.set_delegate, "mallory", w.keys.ph("mallory-hot"), expect="MessageNotSentOrReceived")
        finally:
            undo()
        self.assertIsNone(w.sim.coin_state(decoy.coin_id()).spent_height)
        # the honest spend still lands and spends the real reserve
        w.set_delegate("mallory", w.keys.ph("mallory-hot"))
        self.assertIsNotNone(w.sim.coin_state(real.coin_id()).spent_height)
        self.assertEqual(resync(w.sim, Puzzles(Clvm()), w.launcher_id).distributor.reserve_coin.coin_id(), w.d.reserve_coin.coin_id())

    def test_pinned_a_funded_decoy_is_refused(self):
        w, c, r = funded_world()
        x = w.d.chain_state.reserve
        self.assertGreater(x, 0)
        decoy = plant_decoy(w, x)
        undo = spend_decoy_as_reserve(w, decoy)
        try:
            refused(self, w.claim, r, c.id, "alice", expect="MessageNotSentOrReceived")
        finally:
            undo()
        self.assertGreater(w.claim(r, c.id, "alice"), 0)

    def test_pinned_the_parent_cannot_be_recurried(self):
        # Mallory reveals the finalizer curried with her decoy's parent: the coin's puzzle hash
        # commits to the honest one, so consensus refuses the reveal.
        w = SimWorld(delegate_fee=0)
        w.advance_to(w.h + 3)
        decoy = plant_decoy(w, 0)
        real = w.d.reserve_coin
        w.d.reserve_coin = decoy  # the driver curries (and spends) whatever reserve it holds
        try:
            refused(self, w.set_delegate, "mallory", w.keys.ph("mallory-hot"), expect="WrongPuzzleHash")
        finally:
            w.d.reserve_coin = real

    def test_upstream_resync_follows_a_free_decoy(self):
        # A distributor launched before the fix: the chain accepts the decoy (the B2 finding), and
        # rebuild-from-chain now follows the reserve the spend messaged instead of raising for good.
        w = SimWorld(delegate_fee=0, finalizer=FINALIZER_UPSTREAM)
        w.advance_to(w.h + 3)
        real = w.d.reserve_coin
        decoy = plant_decoy(w, 0)
        undo = spend_decoy_as_reserve(w, decoy)
        w.d.reserve_coin = decoy  # mallory's builder; apply() then follows her lineage
        try:
            w.set_delegate("mallory", w.keys.ph("mallory-hot"))
        finally:
            undo()
        self.assertIsNone(w.sim.coin_state(real.coin_id()).spent_height)  # the launch reserve is stranded
        got = resync(w.sim, Puzzles(Clvm()), w.launcher_id)  # raised ResyncError before the fix
        self.assertEqual(got.distributor.finalizer_kind, FINALIZER_UPSTREAM)
        self.assertEqual(got.distributor.reserve_coin.coin_id(), w.d.reserve_coin.coin_id())
        self.assertEqual(len(got.reserve_forks), 1)
        # the rebuilt driver keeps working: an honest spend lands through it
        w.d = got.distributor
        w.set_delegate("alice", w.keys.ph("alice-hot"))
        self.assertEqual(resync(w.sim, Puzzles(Clvm()), w.launcher_id).distributor.reserve_coin.coin_id(), w.d.reserve_coin.coin_id())

    def test_upstream_resync_follows_a_funded_decoy(self):
        w, c, r = funded_world(FINALIZER_UPSTREAM)
        x = w.d.chain_state.reserve
        real = w.d.reserve_coin
        decoy = plant_decoy(w, x)
        undo = spend_decoy_as_reserve(w, decoy)
        w.d.reserve_coin = decoy
        try:
            w.set_delegate("mallory", w.keys.ph("mallory-hot"))
        finally:
            undo()
        self.assertIsNone(w.sim.coin_state(real.coin_id()).spent_height)  # x stranded, paid by mallory
        got = resync(w.sim, Puzzles(Clvm()), w.launcher_id)
        self.assertEqual(got.distributor.reserve_coin.coin_id(), w.d.reserve_coin.coin_id())
        w.d = got.distributor
        self.assertGreater(w.claim(r, c.id, "alice"), 0)  # holders are paid from mallory's coin


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestPinnedCatReserve(unittest.TestCase):
    def test_a_cat_distributor_launches_claims_and_resyncs_on_the_pinned_finalizer(self):
        w, c, r = funded_world(reward_asset=b"\xca" * 32)
        self.assertTrue(w.d.pinned)
        self.assertGreater(w.claim(r, c.id, "alice"), 0)
        got = resync(w.sim, Puzzles(Clvm()), w.launcher_id)
        self.assertEqual(got.distributor.reserve_coin.coin_id(), w.d.reserve_coin.coin_id())
        self.assertEqual(got.distributor.coin.coin_id(), w.d.coin.coin_id())


def respell(n: int) -> bytes:
    """`n` with a redundant leading zero byte: the same integer, another atom."""
    return b"\x00" + n.to_bytes((n.bit_length() + 8) // 8, "big", signed=True)


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestCP08AndCE02CanonicalAtoms(unittest.TestCase):
    """CP-08 (Low) and CE-02's leaf half: `create` and `post_root` store only canonical integers
    and 32-byte ids; `claim` and `delegate` take `insert` only as 0 or 1. Each case runs the
    compiled leaf on the honest solution (passes) and on the respelled one (refused). Each new
    assert is pinned by scripts/mutate_b2_fixes.py."""

    def assert_only_honest_runs(self, w, action, index, bad_values):
        w.d.simulate([action])  # the honest solution runs
        for bad in bad_values:
            with self.subTest(index=index, value=repr(bad)[:40]):
                sol = list(action.solution)
                sol[index] = bad
                refused(self, w.d.simulate, [type(action)(action.leaf, sol)])

    def test_create_stores_canonical_integers_and_a_32_byte_target(self):
        # claim_window above its floor: AT the floor the range check's `=` already refuses a
        # respelled atom, and the new assert would never be reached (mutation run, 2026-11-13).
        w = SimWorld(delegate_fee=0)
        w.advance_to(w.h + 3)
        start = w.h + HEIGHT_WINDOW + 2
        c = w.campaign("creator", 1_000_000, start, start + 40, Kind.LP_HOLD, POOL, claim_window=200)
        action = w.d.create(w.h, c.creator, c.amount, c.start, c.end, int(c.kind), c.target, c.params.as_clvm(),
                            c.nonce, c.claim_window).action
        self.assert_only_honest_runs(w, action, 2, [respell(c.amount)])
        self.assert_only_honest_runs(w, action, 3, [respell(c.start)])
        self.assert_only_honest_runs(w, action, 4, [respell(c.end)])
        self.assert_only_honest_runs(w, action, 9, [respell(c.claim_window)])
        self.assert_only_honest_runs(w, action, 6, [POOL[:31], POOL + b"x"])

    def test_post_root_stores_canonical_fields(self):
        # Root 1: nothing earlier pins its `start`, and a total above the previous one (0) is
        # range-checked only, so these reach the new asserts (a root 2 is refused earlier by
        # `start == active.end` and an equal total's `=`).
        w = SimWorld(delegate_fee=0)
        w.advance_to(w.h + 3)
        alice = w.keys.ph("alice")
        src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, alice, 1)]})
        start = w.h + HEIGHT_WINDOW + 2
        c = w.campaign("creator", 1_000_000, start, start + 40, Kind.LP_HOLD, POOL, claim_window=160)
        w.create(c, "creator")
        end = start + 20
        w.advance_to(end + 1)
        _, r = run_epoch(EngineState(w.launcher_id, height=start), [c], src, end)
        # One mojo under the schedule's ceiling: a total AT the ceiling is refused by `<=`'s atom
        # `=` already, before the new assert (mutation run, 2026-11-13).
        totals = [(k, v - 1) for k, v in sorted(r.totals.items())]
        action = w.d.post_root(w.h, 1, r.start, r.end, r.root, totals).action
        self.assert_only_honest_runs(w, action, 2, [respell(r.start)])
        self.assert_only_honest_runs(w, action, 3, [respell(r.end)])
        self.assert_only_honest_runs(w, action, 4, [r.root[:31], r.root + b"x"])
        (cid, total), = totals
        self.assert_only_honest_runs(w, action, 5, [[(cid, respell(total))]])

    def test_claim_takes_insert_only_as_0_or_1(self):
        w, c, r = funded_world()
        call = w.claim_call(r, c.id, w.keys.ph("alice"))  # a first claim: insert = 1
        self.assert_only_honest_runs(w, call.action, 5, [b"\x00", b"\x02", b"\x00\x01"])
        self.assertGreater(w.claim(r, c.id, "alice"), 0)

    def test_delegate_takes_insert_only_as_0_or_1(self):
        from flpcr.chain.auth import ByMessage
        w = SimWorld(delegate_fee=0)
        w.advance_to(w.h + 3)
        action = w.d.delegate(w.h, w.keys.ph("alice"), w.keys.ph("hot"), 1, ByMessage()).action
        self.assert_only_honest_runs(w, action, 4, [b"\x00", b"\x02", b"\x00\x01"])


if __name__ == "__main__":
    unittest.main()
