"""The distributor puzzles on the simulator.

Every scenario here also exists in tests/test_distributor.py against the
Python reference model, with the same numbers, so the two must agree.
Skipped when chia-wallet-sdk or the compiled puzzles are not available.

Every test class runs twice: once paying rewards in XCH, and once in a CAT
(the *Cat twins generated at the bottom), so no probe skips the CAT deposit
path by accident.
"""

import unittest

try:
    from chia_wallet_sdk import Coin, CoinSpend

    from flpcr.chain.auth import ByAdmin, ByDelegate, ByMessage, BySignature, Message
    from flpcr.chain.driver import (
        HEIGHT_WINDOW, Action, BundleCollision, ClaimRecord, ClaimRequest, DelegationRecord, Distributor, LeafCall,
    )
    from flpcr.chain.sim import SimWorld
    from flpcr.chain.values import to_program
    from flpcr.clvm import sha256tree

    HAVE_CHAIN = True
except (ImportError, FileNotFoundError):  # pragma: no cover
    HAVE_CHAIN = False

from flpcr import EngineState, FixtureSource, HolderEvent, Kind, run_epoch

POOL, LP = b"\xb0" * 32, b"\x1b" * 32
WINDOW = 56  # the testnet floor: 3 * dispute_blocks + HEIGHT_WINDOW
LEAD = 32  # a campaign starts at least HEIGHT_WINDOW blocks after its create's `h`
CAT_ASSET = b"\xa5" * 32  # a reward CAT; every test class also runs with it below

# Puzzle refusals all surface as `clvm raise`; consensus refusals name their rule.
CLVM_RAISE = "clvm raise"


def refused(test, fn, *args, expect=None, **kwargs):
    """A genuine refusal only: the leaf raised when run (`clvm raise`) or
    consensus rejected the bundle (`Simulator error`). A Python error in the
    test or driver is a bug, not a refusal, and fails the test. `expect` pins
    which: CLVM_RAISE for a puzzle rule, or a consensus error name such as
    "AssertHeightAbsoluteFailed". A `clvm raise` carries no rule, so a probe
    that must hit one particular assertion is pinned by scripts/mutate.py."""
    with test.assertRaises(ValueError) as ctx:
        fn(*args, **kwargs)
    message = str(ctx.exception)
    test.assertTrue("clvm raise" in message or "Simulator error" in message, message)
    if expect is not None:
        test.assertIn(expect, message)


def lifecycle_src(*holders):
    return FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, ph, n) for ph, n in holders]})


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class ChainTest(unittest.TestCase):
    ASSET = bytes(32)  # XCH

    def setUp(self):
        self.w = self.world()
        w = self.w
        w.advance_to(w.h + 3)
        self.camp = w.campaign("creator", 1_000_000, w.h + LEAD + 2, w.h + LEAD + 42, Kind.LP_HOLD, POOL,
                               claim_window=WINDOW)
        self.alice, self.bob = w.keys.ph("alice"), w.keys.ph("bob")
        self.src = lifecycle_src((self.alice, 3), (self.bob, 1))

    def world(self, **kw):
        return SimWorld(reward_asset=self.ASSET, **kw)

    def engine(self, campaign=None):
        c = campaign or self.camp
        return EngineState(self.w.launcher_id, height=c.start)

    def created(self):
        self.w.create(self.camp, "creator")

    def root_at(self, state, end):
        self.w.advance_to(end + 1)
        return run_epoch(state, [self.camp], self.src, end)

    def settle(self):
        """Past the dispute window of a root posted at the current height."""
        self.w.advance_to(self.w.h + HEIGHT_WINDOW + self.w.cfg.dispute_blocks)

    def active(self, root):
        self.w.post(root)
        self.settle()


class TestLifecycle(ChainTest):
    def test_fee_claims_over_time_and_leftovers_to_creator(self):
        w, c = self.w, self.camp
        self.created()
        st = w.state()
        self.assertEqual(st.reserve, 970_000)
        self.assertEqual(st.campaign(c.id).budget, 970_000)
        self.assertEqual(w.reserve_on_chain(), 970_000)
        self.assertEqual(w.balance("dev"), 30_000)  # the 3% dev fee, paid in the create spend

        state, r1 = self.root_at(self.engine(), c.start + 20)
        w.post(r1)
        refused(self, w.claim, r1, c.id, "alice", expect=CLVM_RAISE)  # still in its dispute window
        self.settle()
        self.assertEqual(w.claim(r1, c.id, "alice"), 363_750)
        refused(self, w.claim, r1, c.id, "alice", expect=CLVM_RAISE)  # nothing new

        state, r2 = self.root_at(state, c.end)
        self.active(r2)
        self.assertEqual(w.claim(r2, c.id, "alice"), 363_750)

        if w.h < c.end + WINDOW:
            refused(self, w.reclaim, c.id, expect=CLVM_RAISE)  # window still open
        w.advance_to(c.end + WINDOW)
        self.assertEqual(w.reclaim(c.id), 242_500)  # Bob never claimed
        refused(self, w.claim, r2, c.id, "bob", expect=CLVM_RAISE)  # reclaimed: nothing left to claim from
        self.assertEqual(w.state().reserve, 0)
        self.assertEqual(w.reserve_on_chain(), 0)

    def test_late_claim_before_the_creator_reclaims(self):
        w, c = self.w, self.camp
        self.created()
        _, r = self.root_at(self.engine(), c.end)
        self.active(r)
        w.advance_to(c.end + 2 * WINDOW)  # window long past, creator has not reclaimed
        self.assertEqual(w.claim(r, c.id, "bob"), 242_500)
        self.assertEqual(w.reclaim(c.id), 727_500)  # only Alice's share was left
        refused(self, w.claim, r, c.id, "alice")

    def test_reserve_matches_state_after_every_step(self):
        w, c = self.w, self.camp
        self.created()
        state, r1 = self.root_at(self.engine(), c.start + 10)
        self.active(r1)
        for holder in ("bob", "alice"):
            w.claim(r1, c.id, holder)
            st = w.state()
            self.assertEqual(st.reserve, st.owed)
            self.assertEqual(w.reserve_on_chain(), st.reserve)

    def test_wizard_gated_and_boosted_campaign(self):
        """Gates and boosts live in the params the campaign id commits to; the
        puzzle reads only the first two fields, so a gated campaign creates and
        pays like any other, with the engine's split."""
        from flpcr import Params
        from flpcr.model import Boost, Holding, Requirement

        w = self.w
        wizards, spell = b"\x33" * 32, b"\x5e" * 32
        params = Params(
            gates=(Requirement(Holding.NFT, wizards),),
            boosts=(Boost(Requirement(Holding.CAT, spell, 500), 10000),),
        )
        c = w.campaign("creator", 1_000_000, w.h + LEAD + 2, w.h + LEAD + 42, Kind.LP_HOLD, POOL,
                       claim_window=WINDOW, params=params)
        carol = w.keys.ph("carol")
        src = FixtureSource(
            lp_assets={POOL: LP},
            events={LP: [HolderEvent(0, self.alice, 1), HolderEvent(0, self.bob, 1), HolderEvent(0, carol, 1)],
                    spell: [HolderEvent(0, self.alice, 500)]},
            nfts={wizards: [HolderEvent(0, self.alice, 1), HolderEvent(0, self.bob, 1)]},
        )
        w.create(c, "creator")
        w.advance_to(c.end + 1)
        _, r = run_epoch(self.engine(c), [c], src, c.end)
        self.active(r)
        self.assertEqual(w.claim(r, c.id, "alice"), 646_666)  # 2x boost: 2/3 of 970,000
        self.assertEqual(w.claim(r, c.id, "bob"), 323_333)
        self.assertIsNone(r.claim_for(c.id, carol))  # no Wizard, no leaf

    def test_several_actions_in_one_spend(self):
        w, c = self.w, self.camp
        self.created()
        _, r = self.root_at(self.engine(), c.end)
        self.active(r)
        new_engine = w.keys.ph("engine2")
        w.push([w.claim_call(r, c.id, self.alice), w.d.rotate(w.h, new_engine)])
        self.assertEqual(w.balance("alice"), 727_500)
        self.assertEqual(w.state().rotation.engine_ph, new_engine)  # proposed, not yet in effect


class TestCreateRefusals(ChainTest):
    def test_needs_the_routers_message(self):
        refused(self, self.w.create, self.camp, "creator", router="stranger", expect="MessageNotSentOrReceived")

    def test_needs_the_deposit(self):
        # Conservation is over the whole bundle: the reserve is always funded in
        # full, by someone. With no slack in the bundle, one mojo short fails.
        refused(self, self.w.create, self.camp, "creator", deposit=self.camp.amount - 1)
        self.w.create(self.camp, "creator", deposit=self.camp.amount)
        self.assertEqual(self.w.reserve_on_chain(), self.camp.budget)
        self.assertEqual(self.w.balance("dev"), self.camp.fee)

    def test_campaign_length_bounds(self):
        w = self.w
        s = w.h + LEAD + 2
        refused(self, w.create, w.campaign("creator", 1000, s, s + 15, Kind.LP_HOLD, POOL, claim_window=WINDOW),
                "creator", expect=CLVM_RAISE)
        w.create(w.campaign("creator", 1000, s, s + 16, Kind.LP_HOLD, POOL, claim_window=WINDOW), "creator")

    def test_duplicate_campaign(self):
        self.created()
        refused(self, self.w.create, self.camp, "creator", expect=CLVM_RAISE)

    def test_init_runs_once(self):
        refused(self, self.w.push, [self.w.d.init(self.w.h)], expect=CLVM_RAISE)


class TestCreatorConsents(ChainTest):
    """FLPCR-07: the creator agrees to its own campaign by spending a coin of
    its own; the router's message alone no longer creates one."""

    def test_someone_else_cannot_name_the_creator(self):
        refused(self, self.w.create, self.camp, "creator", consent="mallory", expect="MessageNotSentOrReceived")
        self.w.create(self.camp, "creator")
        self.assertEqual(self.w.state().campaign(self.camp.id).creator, self.w.keys.ph("creator"))

    def test_consent_by_signature_is_refused(self):
        w = self.w
        wallet = w.bls()
        call = w.d.create(w.h, wallet.puzzle_hash, 1000, w.h + LEAD + 2, w.h + LEAD + 40, 1, POOL,
                          [10000, 0, 0, None, None, 1], 0, WINDOW, auth=BySignature(wallet.pk.to_bytes()))
        refused(self, w.push, [call], [lambda: w.fee_spend(1000)], expect=CLVM_RAISE)

    def test_creator_must_be_32_bytes(self):
        for creator in (b"", b"\x11" * 31, b"\x11" * 33):
            refused(self, TestAssertionProbes.raw, self, creator_ph=creator, expect=CLVM_RAISE)


class TestStartAndWindow(ChainTest):
    """FLPCR-15: no campaign starts inside the HEIGHT_WINDOW slack; FLPCR-04:
    none starts further ahead than the longest campaign; FLPCR-08: the claim
    window leaves room for the final root."""

    def raw(self, **kw):
        return TestAssertionProbes.raw(self, **kw)

    def test_start_lead(self):
        w = self.w
        h = w.h
        refused(self, self.raw, start=h + LEAD - 1, end=h + LEAD + 40, h=h, expect=CLVM_RAISE)
        self.raw(start=h + LEAD, end=h + LEAD + 40, h=h)

    def test_start_horizon(self):
        w = self.world(max_campaign_length=200)
        h = w.h
        f = dict(amount=1000, kind=1, target=POOL, params=[10000, 0, 0, None, None, 1], claim_window=WINDOW, h=h)
        refused(self, w.create_raw, "creator", start=h + 201, end=h + 241, nonce=0, expect=CLVM_RAISE, **f)
        w.create_raw("creator", start=h + 200, end=h + 240, nonce=1, **f)

    def test_claim_window_floor(self):
        refused(self, self.raw, claim_window=WINDOW - 1, expect=CLVM_RAISE)
        self.raw(claim_window=WINDOW)


class TestRootRefusals(ChainTest):
    def setUp(self):
        super().setUp()
        self.created()
        self.state, self.r1 = self.root_at(self.engine(), self.camp.start + 20)

    def test_only_the_operator_posts(self):
        refused(self, self.w.post, self.r1, sender="stranger", expect="MessageNotSentOrReceived")

    def test_emission_ceiling(self):
        c = self.camp
        refused(self, self.w.post, self.r1, totals={c.id: c.emittable(self.r1.end) + 1}, expect=CLVM_RAISE)
        self.w.post(self.r1, totals={c.id: c.emittable(self.r1.end)})

    def test_one_pending_at_a_time_and_sequence(self):
        w = self.w
        w.post(self.r1)
        _, r2 = run_epoch(self.state, [self.camp], self.src, self.camp.start + 25)
        w.advance_to(r2.end + 1)
        refused(self, w.post, r2, expect=CLVM_RAISE)  # r1 still in its window
        self.settle()
        refused(self, w.post, r2, root_id=3, expect=CLVM_RAISE)
        w.post(r2)

    def test_totals_never_fall(self):
        w, c = self.w, self.camp
        self.active(self.r1)
        _, r2 = self.root_at(self.state, c.start + 30)
        refused(self, w.post, r2, totals={c.id: self.r1.totals[c.id] - 1}, expect=CLVM_RAISE)

    def test_guardian_veto(self):
        w, c = self.w, self.camp
        w.post(self.r1)
        refused(self, w.veto, 1, sender="stranger", expect="MessageNotSentOrReceived")
        w.veto(1)
        self.settle()
        refused(self, w.claim, self.r1, c.id, "alice", expect=CLVM_RAISE)  # never became active
        w.post(self.r1)  # corrected root, same id
        self.settle()
        refused(self, w.veto, 1, expect=CLVM_RAISE)  # too late once active
        self.assertGreater(w.claim(self.r1, c.id, "alice"), 0)


class TestDisputeWindow(ChainTest):
    """FLPCR-03: a root is never claimable fewer than dispute_blocks after the
    block that includes its post, however far the post's `h` trails it."""

    def test_lagged_h_keeps_the_window(self):
        w, c = self.w, self.camp
        self.created()
        _, r = self.root_at(self.engine(), c.start + 20)
        w.advance_to(w.h + HEIGHT_WINDOW)
        tip = w.h
        lagged = tip - (HEIGHT_WINDOW - 1)  # the most a solution can understate h
        w.post(r, h=lagged)
        claimable_at = lagged + HEIGHT_WINDOW + w.cfg.dispute_blocks
        self.assertGreaterEqual(claimable_at - tip, w.cfg.dispute_blocks)
        w.advance_to(claimable_at - 1)
        refused(self, w.claim, r, c.id, "alice", expect=CLVM_RAISE)
        w.advance_to(claimable_at)
        self.assertGreater(w.claim(r, c.id, "alice"), 0)


class TestHeightPin(ChainTest):
    """FLPCR-05: the two emitted height conditions are what keep `h` honest."""

    def raw(self, **kw):
        return TestAssertionProbes.raw(self, **kw)

    def test_h_cannot_be_in_the_future(self):
        h = self.w.h
        refused(self, self.raw, h=h + 5, start=h + 5 + LEAD, end=h + 5 + LEAD + 40,
                expect="AssertHeightAbsoluteFailed")

    def test_h_cannot_be_stale(self):
        w = self.w
        w.advance_to(w.h + HEIGHT_WINDOW + 2)
        h = w.h - HEIGHT_WINDOW  # the next block is h + 33: one past the window
        refused(self, self.raw, h=h, start=w.h + LEAD, end=w.h + LEAD + 40, expect="AssertBeforeHeightAbsoluteFailed")

    def test_an_early_reclaim_needs_a_future_h(self):
        """The creator authorizes a reclaim naming an `h` past the window: only
        the height pin can refuse it (review probe F/pinprobe.py)."""
        w, c = self.w, self.camp
        self.created()
        _, r = self.root_at(self.engine(), c.end)
        self.active(r)
        call = w.d.reclaim(c.end + c.claim_window, c.id, ByMessage())
        refused(self, w.push, [call], expect="AssertHeightAbsoluteFailed")


class TestRotation(ChainTest):
    """FLPCR-06: a rotated operator key waits rotate_delay blocks (plus the
    HEIGHT_WINDOW slack), in public, and the admin can cancel it."""

    def setUp(self):
        super().setUp()
        self.created()
        self.state, self.r1 = self.root_at(self.engine(), self.camp.start + 20)

    def delay(self):
        return HEIGHT_WINDOW + self.w.cfg.rotate_delay

    def test_rotation_waits(self):
        w = self.w
        refused(self, w.rotate, "engine2", sender="stranger", expect="MessageNotSentOrReceived")
        w.rotate("engine2")
        at = w.h + self.delay()
        refused(self, w.post, self.r1, sender="engine2", expect="MessageNotSentOrReceived")
        w.post(self.r1)  # the current operator still posts
        w.advance_to(at)
        _, r2 = run_epoch(self.state, [self.camp], self.src, self.camp.start + 30)
        self.settle()
        refused(self, w.post, r2, sender="engine", expect="MessageNotSentOrReceived")
        w.post(r2, sender="engine2")
        self.assertEqual(w.state().engine_ph, w.keys.ph("engine2"))

    def test_guardian_cannot_rotate_and_post_in_one_spend(self):
        w = self.w
        guardian = w.cfg.guardian_ph
        totals = sorted(self.r1.totals.items())
        calls = [w.d.rotate(w.h, guardian),
                 w.d.post_root(w.h, 1, self.r1.start, self.r1.end, self.r1.root, totals, sender=guardian)]
        refused(self, w.push, calls, expect="MessageNotSentOrReceived")

    def test_admin_cancels(self):
        w = self.w
        w.rotate("engine2")
        refused(self, w.cancel_rotation, "engine2", sender="stranger", expect="MessageNotSentOrReceived")
        refused(self, w.cancel_rotation, "engine3", expect=CLVM_RAISE)  # names another key
        w.cancel_rotation("engine2")
        self.assertIsNone(w.state().rotation)
        w.advance_to(w.h + self.delay())
        refused(self, w.post, self.r1, sender="engine2", expect="MessageNotSentOrReceived")
        w.post(self.r1)

    def test_cancel_flag_is_zero_or_one(self):
        w = self.w
        w.rotate("engine2")
        call = w.d.cancel_rotation(w.h, w.keys.ph("engine2"))
        call.action.solution[2] = 2
        refused(self, w.push, [call], expect=CLVM_RAISE)

    def test_nothing_to_cancel(self):
        refused(self, self.w.cancel_rotation, "engine2", expect=CLVM_RAISE)

    def test_too_late_to_cancel(self):
        w = self.w
        w.rotate("engine2")
        w.advance_to(w.h + self.delay())
        refused(self, w.cancel_rotation, "engine2", expect=CLVM_RAISE)  # adopted by this spend's prologue

    def test_new_key_must_be_32_bytes(self):
        w = self.w
        call = w.d.rotate(w.h, b"\x22" * 31)
        refused(self, w.push, [call], expect=CLVM_RAISE)


class TestClaimRefusals(ChainTest):
    def setUp(self):
        super().setUp()
        self.created()
        _, self.r = self.root_at(self.engine(), self.camp.end)
        self.active(self.r)

    def test_forged_amount_and_holder(self):
        w, c = self.w, self.camp
        cl = self.r.claim_for(c.id, self.bob)
        refused(self, w.claim, self.r, c.id, "bob", cumulative=cl.cumulative + 1, expect=CLVM_RAISE)
        # Bob's proof, paid to a stranger who authorizes it properly, so only
        # the proof can refuse it.
        stranger = w.keys.ph("stranger")
        call = w.d.claim(w.h, c.id, stranger, cl.cumulative, cl.proof)
        refused(self, w.push, [call], expect=CLVM_RAISE)

    def test_first_claim_cannot_insert_a_second_slot(self):
        """Built so only the duplicate-slot structure can refuse it: a whale
        leaves room under the posted total for Alice's second payout
        (review FLPCR-16: without the whale the posted-total check fired
        first)."""
        w = self.world()
        w.advance_to(w.h + 3)
        c = w.campaign("creator", 1_000_000, w.h + LEAD + 2, w.h + LEAD + 42, Kind.LP_HOLD, POOL,
                       claim_window=WINDOW)
        src = lifecycle_src((w.keys.ph("alice"), 1), (w.keys.ph("whale"), 1000))
        w.create(c, "creator")
        w.advance_to(c.end + 1)
        _, r = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.end)
        w.post(r)
        w.advance_to(w.h + HEIGHT_WINDOW + w.cfg.dispute_blocks)
        alice = w.keys.ph("alice")
        w.claim(r, c.id, "alice")
        key = Distributor.claim_key(c.id, alice)
        saved = w.d.claims.pop(key)  # pretend Alice has no slot yet: an insert between her real neighbours
        try:
            refused(self, w.claim, r, c.id, "alice", expect="MessageNotSentOrReceived")
        finally:
            w.d.claims[key] = saved

    def test_claim_is_paid_once_per_cumulative(self):
        w, c = self.w, self.camp
        self.assertEqual(w.claim(self.r, c.id, "bob"), 242_500)
        refused(self, w.claim, self.r, c.id, "bob", expect=CLVM_RAISE)

    def test_holder_must_be_32_bytes(self):
        w, c = self.w, self.camp
        cl = self.r.claim_for(c.id, self.bob)
        for ph in (b"\x11" * 31, b"\x11" * 33):
            call = w.d.claim(w.h, c.id, ph, cl.cumulative, cl.proof)
            call.messages = []
            refused(self, w.push, [call], expect=CLVM_RAISE)


class TestAssertionProbes(ChainTest):
    """One probe per assertion the mutation run (scripts/mutate.py) found
    unpinned. Each is built so that the assertion it names is the first thing
    that can refuse it."""

    PARAMS = [10000, 0, 0, None, None, 1]

    def raw(self, **kw):
        w = self.w
        f = dict(creator="creator", amount=1000, start=w.h + LEAD + 2, end=w.h + LEAD + 40, kind=1, target=POOL,
                 params=list(TestAssertionProbes.PARAMS), nonce=0, claim_window=WINDOW)
        f.update(kw)
        return w.create_raw(**f)

    # --- create ---

    def test_create_baseline_is_accepted(self):
        self.raw()

    def test_create_amount_and_budget(self):
        refused(self, self.raw, amount=0, expect=CLVM_RAISE)
        refused(self, self.raw, amount=-1000, deposit=0, expect=CLVM_RAISE)

    def test_create_max_length(self):
        w = self.w
        refused(self, self.raw, end=w.h + LEAD + 2 + w.cfg.max_campaign_length + 1, expect=CLVM_RAISE)

    def test_create_kind(self):
        refused(self, self.raw, kind=0, expect=CLVM_RAISE)
        refused(self, self.raw, kind=4, expect=CLVM_RAISE)
        self.raw(kind=3)  # NFT_HOLD

    def test_an_nft_campaign_has_no_fee_weighting(self):
        refused(self, self.raw, kind=3, params=[5000, 5000, 0, None, None, 1], expect=CLVM_RAISE)

    def test_create_params(self):
        refused(self, self.raw, params=[10001, -1, 0, None, None, 1], expect=CLVM_RAISE)
        refused(self, self.raw, params=[-1, 10001, 0, None, None, 1], expect=CLVM_RAISE)
        refused(self, self.raw, params=[9999, 0, 0, None, None, 1], expect=CLVM_RAISE)
        refused(self, self.raw, kind=2, params=[5000, 5000, 0, None, None, 1], expect=CLVM_RAISE)
        self.raw(kind=1, params=[5000, 5000, 0, None, None, 1])

    def test_create_claim_window_ceiling(self):
        refused(self, self.raw, claim_window=self.w.cfg.max_campaign_length + 1, expect=CLVM_RAISE)

    def test_create_campaign_cap(self):
        w = self.world(max_campaigns=1)
        p = list(self.PARAMS)
        w.create_raw("creator", 1000, w.h + LEAD + 2, w.h + LEAD + 40, 1, POOL, p, 0, WINDOW)
        refused(self, w.create_raw, "creator", 1000, w.h + LEAD + 2, w.h + LEAD + 40, 1, POOL, p, 1, WINDOW,
                expect=CLVM_RAISE)

    def test_create_needs_init(self):
        """A distributor whose init never ran takes no campaign."""
        w = self.world(init=False)
        p = list(self.PARAMS)
        refused(self, w.create_raw, "creator", 1000, w.h + LEAD + 2, w.h + LEAD + 40, 1, POOL, p, 0, WINDOW,
                expect=CLVM_RAISE)

    # --- init: the curried Config is checked once ---

    def test_init_checks_the_config(self):
        """`raw_config` skips the Python Config's own checks, so only the leaf
        can refuse."""
        for field_name, value in (("rotate_delay", 8), ("delegate_fee", -1), ("admin_ph", b"\x01" * 31),
                                  ("dev_fee_ph", b"\x01" * 33), ("dispute_bond", 0)):
            refused(self, self.world, raw_config={field_name: value}, expect=CLVM_RAISE)

    # --- the height pin ---

    def test_height_must_be_positive(self):
        self.assertLess(self.w.h, 32)  # below 32, ASSERT_BEFORE_HEIGHT h+32 cannot catch h = 0
        refused(self, self.raw, h=0, start=LEAD + 2, end=LEAD + 40, expect=CLVM_RAISE)

    def test_one_height_per_spend(self):
        w, c = self.w, self.camp
        self.created()
        _, r = self.root_at(self.engine(), c.end)
        self.active(r)
        claim = w.claim_call(r, c.id, self.bob)
        early = w.d.reclaim(w.h + c.claim_window + 100, c.id, ByMessage())  # a later action claiming a later height
        refused(self, w.push, [claim, early], expect=CLVM_RAISE)

    # --- post_root ---

    def posted_setup(self):
        self.created()
        return self.root_at(self.engine(), self.camp.start + 20)

    def test_post_cannot_replace_a_pending_root(self):
        w, c = self.w, self.camp
        state, r1 = self.posted_setup()
        w.post(r1)
        refused(self, w.post, r1, totals={c.id: 1}, expect=CLVM_RAISE)  # same id, different totals, still pending

    def test_post_window_and_future(self):
        w, c = self.w, self.camp
        state, r1 = self.posted_setup()
        refused(self, w.post, r1, start=r1.end, end=r1.end, expect=CLVM_RAISE)
        future = w.h + 50
        refused(self, w.post, r1, end=future, totals={c.id: c.emittable(future)}, expect=CLVM_RAISE)

    def test_post_first_id_and_tiling(self):
        w, c = self.w, self.camp
        state, r1 = self.posted_setup()
        refused(self, w.post, r1, root_id=2, expect=CLVM_RAISE)
        self.active(r1)
        _, r2 = self.root_at(state, c.start + 30)
        refused(self, w.post, r2, start=r1.end + 1, expect=CLVM_RAISE)
        refused(self, w.post, r2, start=r1.end - 1, expect=CLVM_RAISE)
        w.post(r2)

    def test_post_totals_sorted_unique_and_kept(self):
        w, c = self.w, self.camp
        state, r1 = self.posted_setup()
        t = r1.totals[c.id]
        refused(self, w.post, r1, totals_list=[(c.id, t), (c.id, t)], expect=CLVM_RAISE)
        self.active(r1)
        _, r2 = self.root_at(state, c.start + 30)
        refused(self, w.post, r2, totals_list=[], expect=CLVM_RAISE)  # a live campaign may not be dropped

    def test_post_unknown_campaign(self):
        w = self.w
        state, r1 = self.posted_setup()
        refused(self, w.post, r1, totals_list=[(b"\x77" * 32, 1)], expect=CLVM_RAISE)

    # --- veto ---

    def test_veto_names_the_pending_root(self):
        w = self.w
        state, r1 = self.posted_setup()
        w.post(r1)
        refused(self, w.veto, 2, expect=CLVM_RAISE)
        w.veto(1)

    # --- claim ---

    def test_claims_cannot_exceed_the_posted_total(self):
        w, c = self.w, self.camp
        self.created()
        _, r = self.root_at(self.engine(), c.end)
        w.post(r, totals={c.id: r.totals[c.id] - 1})  # leaves sum to one more than the stated total
        self.settle()
        w.claim(r, c.id, "alice")
        refused(self, w.claim, r, c.id, "bob", expect=CLVM_RAISE)

    def test_insert_must_be_ordered(self):
        """A second slot for an existing key, placed between two other
        neighbours, would reset the holder's claimed total and pay again.

        Built so the ordering check is the only thing that can refuse: Alice
        holds little and has claimed; a large holder ("whale") has not, so the
        campaign's posted total has room for Alice's duplicate. Several small
        holders claim first so there are neighbour pairs on both sides of
        Alice's key."""
        w, c = self.w, self.camp
        alice_key = Distributor.claim_key(c.id, self.alice)
        below = [f"s{i}" for i in range(64) if Distributor.claim_key(c.id, w.keys.ph(f"s{i}")) < alice_key][:3]
        above = [f"s{i}" for i in range(64) if Distributor.claim_key(c.id, w.keys.ph(f"s{i}")) > alice_key][:3]
        small = below + above
        self.src = FixtureSource(
            lp_assets={POOL: LP},
            events={LP: [HolderEvent(0, w.keys.ph(x), 1) for x in ["alice", *small]]
                    + [HolderEvent(0, w.keys.ph("whale"), 1000)]},
        )
        self.created()
        _, r = self.root_at(self.engine(), c.end)
        self.active(r)
        for holder in ["alice", *small]:
            w.claim(r, c.id, holder)
        d = w.d
        keys = sorted(d.claims)
        cl = r.claim_for(c.id, self.alice)
        sides = set()
        for lk, rk in zip(keys, keys[1:]):
            if lk <= alice_key <= rk:
                continue
            sides.add("left" if rk < alice_key else "right")
            L, R = d.claims[lk], d.claims[rk]
            call = w.d.claim(w.h, c.id, self.alice, cl.cumulative, cl.proof)
            call.action.solution[5] = 1
            call.action.solution[6] = [lk, L.claimed, L.left, rk, R.claimed, R.right]
            call.claim_updates = {}
            refused(self, w.push, [call], expect=CLVM_RAISE)
        self.assertEqual(sides, {"left", "right"})


class TestOnlyTheHolderClaims(ChainTest):
    """Rewards are claimed by the holder and nobody else: a coin at the
    holder's address sends the claim (a standard wallet, a vault, a multisig
    or any other puzzle), or the address's synthetic key signs it. Neither
    names a generation of the distributor."""

    def setUp(self):
        super().setUp()
        w = self.w
        self.wallet = w.bls()
        self.other_wallet = w.bls()
        self.src = lifecycle_src((self.alice, 3), (self.bob, 1), (self.wallet.puzzle_hash, 4))
        self.created()
        _, self.r = self.root_at(self.engine(), self.camp.end)
        self.active(self.r)

    def test_no_authorization_is_refused(self):
        """What a keeper would do: claim for Bob without Bob."""
        w, c = self.w, self.camp
        call = w.claim_call(self.r, c.id, self.bob)
        call.messages = []
        refused(self, w.push, [call], expect="MessageNotSentOrReceived")

    def test_someone_elses_message_is_refused(self):
        refused(self, self.w.claim, self.r, self.camp.id, "bob", sender="stranger", expect="MessageNotSentOrReceived")
        self.assertGreater(self.w.claim(self.r, self.camp.id, "bob"), 0)

    def test_standard_wallet_claims_by_spending_its_coin(self):
        w, c = self.w, self.camp
        self.assertEqual(w.claim(self.r, c.id, self.wallet), 485_000)

    def by_key(self, wallet, key=None):
        return self.w.claim_call(self.r, self.camp.id, wallet.puzzle_hash, auth=BySignature((key or wallet).pk.to_bytes()))

    def test_a_key_signature_claims_without_a_coin(self):
        """A wallet's address needs no coin: its key signs the claim leaf."""
        w = self.w
        before = w.balance(self.wallet.puzzle_hash)
        p = w.push([self.by_key(self.wallet)])
        self.assertEqual(w.balance(self.wallet.puzzle_hash) - before, 485_000)
        self.assertEqual(p.messages, [])
        self.assertTrue(all(s.unsafe for s in p.signatures))

    def test_a_signature_is_not_replayed(self):
        """The leaf names the cumulative: the same signature a second time pays nothing."""
        w = self.w
        w.push([self.by_key(self.wallet)])
        refused(self, w.push, [self.by_key(self.wallet)], expect=CLVM_RAISE)

    def test_a_key_for_another_address_is_refused(self):
        refused(self, self.w.push, [self.by_key(self.wallet, key=self.other_wallet)], expect=CLVM_RAISE)

    def test_the_key_must_actually_sign(self):
        """The key matches the address, but another key signs: no aggregate verifies."""
        w = self.w
        call = self.by_key(self.wallet)
        prepared = w.d.prepare([call])
        with self.assertRaises(ValueError):
            w.sim.spend_coins(list(prepared.spends), [self.other_wallet.sk])

    def test_a_claim_names_no_generation(self):
        """The holder's message is built against one generation and lands in a
        later one: a sequencer can place it in whichever spend it builds."""
        w, c = self.w, self.camp
        before = w.balance(self.wallet.puzzle_hash)
        call = w.claim_call(self.r, c.id, self.wallet.puzzle_hash)
        presigned = [s for m in call.messages for s in w.send(m.sender, m.message, m.mode)]
        w.claim(self.r, c.id, "bob")  # the distributor moves on a generation
        call = w.claim_call(self.r, c.id, self.wallet.puzzle_hash)  # same leaf, fresh slot info
        prepared = w.d.prepare([call])
        w.sim.spend_coins(list(prepared.spends) + presigned, [self.wallet.sk])
        w.d.apply(prepared)
        self.assertEqual(w.balance(self.wallet.puzzle_hash) - before, 485_000)

    def test_someone_elses_wallet_is_refused(self):
        w, c = self.w, self.camp
        call = w.claim_call(self.r, c.id, self.wallet.puzzle_hash)
        refused(self, w.push, [call], senders={self.wallet.puzzle_hash: self.other_wallet.puzzle_hash},
                expect="MessageNotSentOrReceived")

    def test_unknown_auth_mode_is_refused(self):
        w, c = self.w, self.camp
        call = w.claim_call(self.r, c.id, self.bob)
        call.action.solution[7] = 3
        refused(self, w.push, [call], expect=CLVM_RAISE)


class TestDelegatedClaims(ChainTest):
    """A cold wallet grants a hot wallet (a cloud wallet, a vault's signer) the
    right to claim for it. Rewards still go to the cold wallet, and the hot
    wallet claims by spending a coin of its own."""

    def setUp(self):
        super().setUp()
        w = self.w
        self.cold = w.bls()
        self.hot = w.bls()
        self.thief = w.bls()
        self.src = lifecycle_src((self.cold.puzzle_hash, 1), (self.bob, 1))
        self.created()
        _, self.r = self.root_at(self.engine(), self.camp.end)
        self.active(self.r)

    def test_hot_wallet_claims_for_cold_wallet(self):
        w, c = self.w, self.camp
        refused(self, w.claim_delegated, self.r, c.id, self.cold.puzzle_hash, self.hot,
                expect="MessageNotSentOrReceived")  # not granted yet
        w.set_delegate(self.cold, self.hot.puzzle_hash)
        hot_before = w.balance(self.hot.puzzle_hash)
        self.assertEqual(w.claim_delegated(self.r, c.id, self.cold.puzzle_hash, self.hot), 485_000)
        self.assertEqual(w.balance(self.hot.puzzle_hash), hot_before)  # paid to the cold wallet, never the hot one

    def test_a_delegate_claims_by_key_signature(self):
        """The delegate's key signs the leaf (no coin at the delegate needed);
        the rewards still go to the owner."""
        w, c = self.w, self.camp
        w.set_delegate(self.cold, self.hot.puzzle_hash)
        hot_before = w.balance(self.hot.puzzle_hash)
        self.assertEqual(w.claim_delegated(self.r, c.id, self.cold.puzzle_hash, self.hot, signed=True), 485_000)
        self.assertEqual(w.balance(self.hot.puzzle_hash), hot_before)

    def test_one_grant_serves_every_later_claim(self):
        """Granted once, mid-campaign; the hot wallet claims after two roots and
        the delegation slot is recreated each time, its use counter rising."""
        w = self.world()
        w.wallets.update({self.cold.puzzle_hash: self.cold, self.hot.puzzle_hash: self.hot})
        w.advance_to(w.h + 3)
        c = w.campaign("creator", 1_000_000, w.h + LEAD + 2, w.h + LEAD + 42, Kind.LP_HOLD, POOL,
                       claim_window=WINDOW)
        w.create(c, "creator")
        w.set_delegate(self.cold, self.hot.puzzle_hash)
        w.advance_to(c.start + 21)
        st, r1 = run_epoch(EngineState(w.launcher_id, height=c.start), [c], self.src, c.start + 20)
        w.post(r1)
        w.advance_to(w.h + HEIGHT_WINDOW + w.cfg.dispute_blocks)
        first = w.claim_delegated(r1, c.id, self.cold.puzzle_hash, self.hot)
        refused(self, w.claim_delegated, r1, c.id, self.cold.puzzle_hash, self.hot, expect=CLVM_RAISE)
        w.advance_to(c.end + 1)
        _, r2 = run_epoch(st, [c], self.src, c.end)
        w.post(r2)
        w.advance_to(w.h + HEIGHT_WINDOW + w.cfg.dispute_blocks)
        second = w.claim_delegated(r2, c.id, self.cold.puzzle_hash, self.hot)
        self.assertEqual((first, first + second), (242_500, 485_000))
        key = Distributor.delegation_key(self.cold.puzzle_hash, self.hot.puzzle_hash)
        self.assertEqual(w.d.delegations[key].uses, 2)

    def test_only_the_granted_delegate(self):
        w, c = self.w, self.camp
        w.set_delegate(self.cold, self.hot.puzzle_hash)
        refused(self, w.claim_delegated, self.r, c.id, self.cold.puzzle_hash, self.thief,
                expect="MessageNotSentOrReceived")

    def test_only_the_holder_grants(self):
        w = self.w
        # The thief tries to grant itself the cold wallet's claims.
        refused(self, w.set_delegate, self.cold, self.thief.puzzle_hash, signer=self.thief, expect=CLVM_RAISE)
        # And by message from a stranger's address on behalf of an address holder.
        refused(self, w.set_delegate, "bob", self.thief.puzzle_hash, signer="stranger",
                expect="MessageNotSentOrReceived")

    def test_revoke(self):
        w, c = self.w, self.camp
        w.set_delegate(self.cold, self.hot.puzzle_hash)
        refused(self, w.set_delegate, self.cold, self.hot.puzzle_hash, grant=0, signer=self.thief, expect=CLVM_RAISE)
        w.set_delegate(self.cold, self.hot.puzzle_hash, grant=0)
        refused(self, w.claim_delegated, self.r, c.id, self.cold.puzzle_hash, self.hot,
                expect="MessageNotSentOrReceived")
        self.assertEqual(w.claim(self.r, c.id, self.cold), 485_000)  # the cold wallet still can

    def test_grant_twice_revoke_once_revokes(self):
        """FLPCR-14: one slot per (owner, delegate), so a repeated grant is an
        update, and one revoke cuts the delegate off."""
        w, c = self.w, self.camp
        w.set_delegate(self.cold, self.hot.puzzle_hash)
        w.set_delegate(self.cold, self.hot.puzzle_hash)
        w.set_delegate(self.cold, self.hot.puzzle_hash, grant=0)
        refused(self, w.claim_delegated, self.r, c.id, self.cold.puzzle_hash, self.hot,
                expect="MessageNotSentOrReceived")
        w.set_delegate(self.cold, self.hot.puzzle_hash)  # granted again: the same slot, active again
        self.assertEqual(w.claim_delegated(self.r, c.id, self.cold.puzzle_hash, self.hot), 485_000)

    def test_revoking_a_never_granted_pair_is_refused(self):
        refused(self, self.w.set_delegate, self.cold, self.hot.puzzle_hash, grant=0, expect=CLVM_RAISE)

    def test_a_second_slot_for_a_granted_key_is_refused(self):
        """Inserting a granted key again, between other neighbours, is refused
        by the ordering check, so a key exists once."""
        w = self.w
        w.set_delegate(self.cold, self.hot.puzzle_hash)
        others = [w.bls() for _ in range(8)]
        for o in others:
            w.set_delegate(self.cold, o.puzzle_hash)
        key = Distributor.delegation_key(self.cold.puzzle_hash, self.hot.puzzle_hash)
        keys = sorted(w.d.delegations)
        sides = set()
        for lk, rk in zip(keys, keys[1:]):
            if lk <= key <= rk:
                continue
            sides.add("left" if rk < key else "right")
            L, R = w.d.delegations[lk], w.d.delegations[rk]
            call = w.d.delegate(w.h, self.cold.puzzle_hash, self.hot.puzzle_hash, 1,
                                BySignature(self.cold.pk.to_bytes()))
            call.action.solution[4] = 1
            call.action.solution[5] = [lk, L.active, L.uses, L.left, rk, R.active, R.uses, R.right]
            call.delegation_updates = {}
            refused(self, w.push, [call], expect=CLVM_RAISE)
        self.assertTrue(sides)

    def test_ungranted_delegate_cannot_prove_itself_with_this_spends_slot(self):
        """FLPCR-02: the delegated use spends (key, 1, uses) and recreates
        (key, 1, uses + 1), so the slot the spend creates can never be the one
        that proves the grant, even spent ephemerally in the same bundle."""
        w, c = self.w, self.camp
        cold, thief = self.cold.puzzle_hash, self.thief.puzzle_hash
        call = w.claim_call(self.r, c.id, cold, auth=ByDelegate(thief, ByMessage()))
        key = Distributor.delegation_key(cold, thief)
        from flpcr.chain.driver import DelegationRecord

        inner_ph = w.d.inner_puzzle(w.d.state).tree_hash()
        lineage = to_program(w.clvm, ((w.d.coin.parent_coin_info, (inner_ph, 1)), inner_ph))

        def ephemeral(uses):
            rec = DelegationRecord(1, uses, b"\x00" * 32, b"\xff" * 32, b"", b"")
            puzzle = w.d.delegation_slot_puzzle(key, rec)
            coin = Coin(w.d.coin.coin_id(), puzzle.tree_hash(), 0)
            return lambda: [CoinSpend(coin, puzzle.serialize(), lineage.serialize())]

        before = w.balance(cold)
        for uses in (0, 1):  # the value the leaf spends, and the value it creates
            call = w.claim_call(self.r, c.id, cold, auth=ByDelegate(thief, ByMessage()))
            refused(self, w.push, [call], [ephemeral(uses)], expect="Simulator error")
        self.assertEqual(w.balance(cold), before)

    def test_grant_is_zero_or_one(self):
        w = self.w
        w.set_delegate(self.cold, self.hot.puzzle_hash)
        call = w.d.delegate(w.h, self.cold.puzzle_hash, self.hot.puzzle_hash, 2, BySignature(self.cold.pk.to_bytes()))
        refused(self, w.push, [call], expect=CLVM_RAISE)  # signed by the holder, still refused

    def test_owner_and_delegate_are_32_bytes(self):
        w = self.w
        for owner, delegate in ((b"\x11" * 31, self.hot.puzzle_hash), (self.bob, b"\x22" * 33)):
            call = w.d.delegate(w.h, owner, delegate, 1, ByMessage())
            call.messages = [Message(self.bob, m.message) for m in call.messages]
            refused(self, w.push, [call], expect=CLVM_RAISE)

    def test_vault_holder_grants_by_message_and_hot_wallet_claims(self):
        """A holder whose address is not a standard wallet (here an
        anyone-can-spend stand-in for a vault) grants once by message; the hot
        wallet then claims by spending its own coin."""
        w, c = self.w, self.camp
        w.set_delegate("bob", self.hot.puzzle_hash)
        self.assertEqual(w.claim_delegated(self.r, c.id, self.bob, self.hot), 485_000)

    def test_delegate_cannot_delegate(self):
        w, c = self.w, self.camp
        w.set_delegate(self.cold, self.hot.puzzle_hash)
        call = w.claim_call(self.r, c.id, self.cold.puzzle_hash, auth=ByDelegate(self.hot.puzzle_hash))
        rec = w.d.delegations[Distributor.delegation_key(self.cold.puzzle_hash, self.hot.puzzle_hash)]
        call.action.solution[8] = (self.hot.puzzle_hash, (rec.uses, (rec.left, (rec.right, (2, None)))))
        refused(self, w.push, [call], expect=CLVM_RAISE)


class TestDelegateFee(ChainTest):
    """FLPCR-12: every grant or revoke leaves delegate_fee mojos as network
    fee, so spamming the singleton through `delegate` costs every block."""

    def world(self, **kw):
        return SimWorld(reward_asset=self.ASSET, delegate_fee=1_000_000, **kw)

    def test_fee_is_required(self):
        w = self.w
        hot = w.bls()
        call = w.d.delegate(w.h, self.bob, hot.puzzle_hash, 1, ByMessage())
        self.assertEqual(call.fee, 1_000_000)
        call.fee = 999_999  # the bundle leaves one mojo less than the leaf reserves
        refused(self, w.push, [call], expect="ReserveFee")
        w.set_delegate("bob", hot.puzzle_hash)  # the full fee: accepted
        self.assertTrue(w.d.is_delegated(self.bob, hot.puzzle_hash))


class TestBatches(ChainTest):
    """FLPCR-13: actions that cannot share one singleton spend are refused by
    the driver, before any node sees them."""

    def setUp(self):
        super().setUp()
        w = self.w
        self.src = lifecycle_src((self.alice, 1), (self.bob, 1))
        self.created()
        self.c2 = w.campaign("creator", 1_000_000, self.camp.start, self.camp.end, Kind.LP_HOLD, POOL,
                             claim_window=WINDOW, nonce=1)
        w.create(self.c2, "creator")
        both = [self.camp, self.c2]
        w.advance_to(self.camp.start + 21)
        self.state, self.r1 = run_epoch(self.engine(), both, self.src, self.camp.start + 20)
        self.active(self.r1)
        w.advance_to(self.camp.end + 1)
        _, self.r2 = run_epoch(self.state, both, self.src, self.camp.end)

    def test_two_first_claims_sharing_a_neighbour(self):
        w = self.w
        calls = [w.claim_call(self.r1, self.camp.id, self.alice), w.claim_call(self.r1, self.camp.id, self.bob)]
        with self.assertRaisesRegex(BundleCollision, "slot"):
            w.d.prepare(calls)

    def test_equal_payouts_to_one_holder(self):
        """Two campaigns paying Alice the same increment: one coin id twice."""
        w = self.w
        w.claim(self.r1, self.camp.id, "alice")
        w.claim(self.r1, self.c2.id, "alice")
        self.active(self.r2)
        calls = [w.claim_call(self.r2, self.camp.id, self.alice), w.claim_call(self.r2, self.c2.id, self.alice)]
        with self.assertRaisesRegex(BundleCollision, "payments"):
            w.d.prepare(calls)
        w.claim(self.r2, self.camp.id, "alice")  # split across spends: both accepted
        w.claim(self.r2, self.c2.id, "alice")

    def test_one_delegate_twice_in_one_spend(self):
        """Bob, Alice's delegate, claims both her campaigns: one delegation
        slot would be recreated twice."""
        w = self.w
        w.claim(self.r1, self.camp.id, "alice")
        w.claim(self.r1, self.c2.id, "alice")
        w.set_delegate("alice", self.bob)
        self.active(self.r2)
        calls = [w.claim_call(self.r2, cid, self.alice, auth=ByDelegate(self.bob)) for cid in (self.camp.id, self.c2.id)]
        with self.assertRaisesRegex(BundleCollision, "delegation"):
            w.d.prepare(calls)


class TestSequencer(ChainTest):
    """Thousands of holders claim through a sequencer: claims name no
    generation, so it packs the ones that share no slot into one generation
    and chains further generations of the singleton in the same bundle
    (scripts/scale_claims.py measures 5,000 holders)."""

    HOLDERS = 24

    def setUp(self):
        super().setUp()
        w = self.w
        self.wallets = [w.bls() for _ in range(self.HOLDERS)]
        self.src = lifecycle_src((self.alice, 3), (self.bob, 1), *[(x.puzzle_hash, 1 + i % 4) for i, x in enumerate(self.wallets)])
        self.created()
        self.state, self.r = self.root_at(self.engine(), self.camp.end)
        self.active(self.r)

    def request(self, ph, auth=None, cumulative=None):
        cl = self.r.claim_for(self.camp.id, ph)
        if auth is None:
            wallet = next(x for x in self.wallets if x.puzzle_hash == ph)
            auth = BySignature(wallet.pk.to_bytes())
        return ClaimRequest(self.camp.id, ph, cl.cumulative if cumulative is None else cumulative, cl.proof, auth)

    def everyone(self):
        return [self.request(x.puzzle_hash) for x in self.wallets] + [self.request(self.alice, ByMessage()), self.request(self.bob, ByMessage())]

    def test_every_first_claim_in_one_bundle(self):
        w = self.w
        reqs = self.everyone()
        before = {r.puzzle_hash: w.balance(r.puzzle_hash) for r in reqs}
        reserve = w.reserve_on_chain()
        batch = w.d.sequence(w.h, reqs)
        self.assertEqual((len(batch.placed), batch.deferred, batch.rejected), (len(reqs), [], []))
        self.assertGreater(len(batch.generations), 1)  # neighbours force chained generations
        w.push_batch(batch)
        for r in reqs:
            self.assertEqual(w.balance(r.puzzle_hash) - before[r.puzzle_hash], r.cumulative, r.puzzle_hash.hex()[:8])
        self.assertEqual(reserve - w.reserve_on_chain(), sum(r.cumulative for r in reqs))
        self.assertEqual(len(w.d.claims) - 2, len(reqs))

    def test_sequencing_leaves_the_driver_where_it_was(self):
        w = self.w
        coin = w.d.coin
        w.d.sequence(w.h, self.everyone())
        self.assertEqual(w.d.coin, coin)
        self.assertEqual(len(w.d.claims), 2)
        self.assertGreater(w.claim(self.r, self.camp.id, "alice"), 0)  # an ordinary claim still builds

    def test_a_bad_claim_is_set_aside(self):
        """A forged cumulative fails its proof: it is rejected, the rest go through."""
        w = self.w
        reqs = self.everyone()
        reqs[3] = self.request(self.wallets[3].puzzle_hash, cumulative=reqs[3].cumulative + 1)
        batch = w.d.sequence(w.h, reqs)
        self.assertEqual([r for r, _ in batch.rejected], [reqs[3]])
        w.push_batch(batch)
        self.assertEqual(len(batch.placed), len(reqs) - 1)

    def test_one_request_per_holder(self):
        w = self.w
        x = self.wallets[0].puzzle_hash
        full = self.request(x)
        batch = w.d.sequence(w.h, [self.request(x, cumulative=full.cumulative - 5), full])
        self.assertEqual(batch.placed, [full])
        w.push_batch(batch)

    def test_the_budget_defers_the_rest(self):
        """A small budget places what fits; the rest goes in the next bundle,
        and repeat claims after a second root batch the same way."""
        w = self.w
        reqs = self.everyone()
        first = w.d.sequence(w.h, reqs, max_cost=400_000_000)
        self.assertTrue(first.placed and first.deferred)
        w.push_batch(first)
        w.sim.create_block()
        second = w.d.sequence(w.h, first.deferred)
        self.assertEqual(second.deferred, [])
        w.push_batch(second)
        self.assertEqual(len(w.d.claims) - 2, len(reqs))


class TestExcludedAddresses(ChainTest):
    """The burn address never claims, and neither does any address the
    creator excluded at launch (params' blacklist, at most 16). The engine
    leaves them out of every share; the claim leaf refuses them even from a
    root that pays them, so an operator's mistake cannot reach them."""

    def setUp(self):
        super().setUp()
        from unittest import mock

        from flpcr import Params
        from flpcr.model import BURN_PUZZLE_HASH, Campaign

        self.mock, self.Campaign, self.BURN = mock, Campaign, BURN_PUZZLE_HASH
        w = self.w
        self.src = lifecycle_src((self.alice, 2), (self.bob, 1), (BURN_PUZZLE_HASH, 1))
        self.camp = w.campaign("creator", 1_000_000, self.camp.start, self.camp.end, Kind.LP_HOLD, POOL,
                               claim_window=WINDOW, params=Params(blacklist=frozenset({self.alice})))
        self.created()

    def root(self, forged=False):
        """The engine's root, or (forged) one that also pays the excluded addresses."""
        if not forged:
            return self.root_at(self.engine(), self.camp.end)[1]
        with self.mock.patch.object(self.Campaign, "eligible", lambda _, ph, balance: balance > 0):
            return self.root_at(self.engine(), self.camp.end)[1]

    def test_the_engine_pays_neither(self):
        r = self.root()
        self.assertEqual({cl.puzzle_hash for cl in r.claims}, {self.bob})
        self.assertEqual(r.claim_for(self.camp.id, self.bob).cumulative, self.camp.budget)  # Bob takes the whole emission

    def test_an_excluded_address_cannot_claim_from_a_root_that_pays_it(self):
        w, r = self.w, self.root(forged=True)
        self.active(r)
        refused(self, w.claim, r, self.camp.id, "alice", expect=CLVM_RAISE)
        self.assertGreater(w.claim(r, self.camp.id, "bob"), 0)

    def test_the_burn_address_never_claims(self):
        w, r = self.w, self.root(forged=True)
        self.active(r)
        call = w.claim_call(r, self.camp.id, self.BURN)
        refused(self, w.push, [call], expect=CLVM_RAISE)

    def test_the_list_must_be_the_campaigns(self):
        """Hiding Alice by revealing a shorter list fails the stored hash."""
        w, r = self.w, self.root(forged=True)
        self.active(r)
        cl = r.claim_for(self.camp.id, self.alice)
        call = w.d.claim(w.h, self.camp.id, self.alice, cl.cumulative, cl.proof, excluded=[])
        refused(self, w.push, [call], expect=CLVM_RAISE)

    def test_create_refuses_a_long_or_malformed_list(self):
        from flpcr import Params
        w = self.w
        for listed in ([bytes([i]) * 32 for i in range(17)], [b"\x01" * 31]):
            params = Params().as_clvm()
            params[3] = listed
            refused(self, w.create_raw, "creator", 1_000_000, self.camp.start, self.camp.end, 1, POOL, params, 9, WINDOW,
                    expect=CLVM_RAISE)
        params = Params().as_clvm()
        params[3] = [bytes([i]) * 32 for i in range(16)]
        w.create_raw("creator", 1_000_000, self.camp.start, self.camp.end, 1, POOL, params, 9, WINDOW)  # 16 is fine


class TestNftCampaigns(ChainTest):
    """An NFT_HOLD campaign pays the holders of a collection (the NFTs a DID
    minted), one share per NFT, time-weighted, nothing staked: the NFTs are
    minted and moved on the simulator and read back from its coins."""

    def setUp(self):
        super().setUp()
        from flpcr.chain.nfts import ChainNftSource, Combined
        from flpcr.chain.sim import NftKit

        w = self.w
        self.kit = NftKit(w.sim, w.clvm)
        self.minter, self.holder_a, self.holder_b = w.bls(10_000), w.bls(), w.bls()
        did = self.kit.create_did(self.minter)
        self.collection = did.info.launcher_id
        self.to_a, did = self.kit.mint(self.minter, did, 2, to_ph=self.holder_a.puzzle_hash)
        self.to_b, did = self.kit.mint(self.minter, did, 1, to_ph=self.holder_b.puzzle_hash)
        # A look-alike from another DID, sent to holder B: not in the collection.
        other = w.bls(10_000)
        impostor = self.kit.create_did(other)
        self.kit.mint(other, impostor, 1, to_ph=self.holder_b.puzzle_hash)
        self.src = Combined(FixtureSource(), ChainNftSource(w.sim, w.clvm))
        w.advance_to(w.h + 2)
        self.camp = w.campaign("creator", 1_000_000, w.h + LEAD + 2, w.h + LEAD + 42, Kind.NFT_HOLD, self.collection,
                               claim_window=WINDOW)
        self.created()

    def claim_by_key(self, r, wallet):
        before = self.w.balance(wallet.puzzle_hash)
        self.w.push([self.w.claim_call(r, self.camp.id, wallet.puzzle_hash, auth=BySignature(wallet.pk.to_bytes()))])
        return self.w.balance(wallet.puzzle_hash) - before

    def test_one_share_per_nft(self):
        _, r = self.root_at(self.engine(), self.camp.end)
        self.active(r)
        self.assertEqual(self.claim_by_key(r, self.holder_a), 646_666)  # 2 of 3 NFTs: 2/3 of 970,000
        self.assertEqual(self.claim_by_key(r, self.holder_b), 323_333)  # the look-alike earns nothing

    def test_a_transfer_moves_the_share_from_that_block(self):
        """Holder A sends one NFT to B halfway: A holds 2 then 1, B 1 then 2."""
        w, c = self.w, self.camp
        w.advance_to(c.start + 19)
        self.kit.transfer(self.holder_a, self.to_a[0], self.holder_b.puzzle_hash)
        _, r = self.root_at(self.engine(), c.end)
        self.active(r)
        a, b = self.claim_by_key(r, self.holder_a), self.claim_by_key(r, self.holder_b)
        self.assertAlmostEqual(a / (a + b), 0.5, delta=0.03)
        self.assertLessEqual(a + b, c.budget)

    def test_the_creator_can_exclude_a_holder(self):
        from flpcr import Params
        w = self.w
        c = w.campaign("creator", 1_000_000, self.camp.start, self.camp.end, Kind.NFT_HOLD, self.collection,
                       claim_window=WINDOW, nonce=7, params=Params(blacklist=frozenset({self.holder_a.puzzle_hash})))
        w.create(c, "creator")
        w.advance_to(c.end + 1)
        _, r = run_epoch(EngineState(w.launcher_id, height=c.start), [c], self.src, c.end)
        self.assertEqual({x.puzzle_hash for x in r.claims}, {self.holder_b.puzzle_hash})
        self.assertEqual(r.claim_for(c.id, self.holder_b.puzzle_hash).cumulative, c.budget)


class TestOnlyTheCreatorReclaims(ChainTest):
    """The leftovers go back to the creator, and only the creator (or a
    delegate the creator granted, or the admin) decides when."""

    def setUp(self):
        super().setUp()
        w = self.w
        self.hot = w.bls()
        self.created()
        _, r = self.root_at(self.engine(), self.camp.end)
        self.active(r)
        w.advance_to(self.camp.end + WINDOW)

    def test_stranger_cannot_reclaim(self):
        refused(self, self.w.reclaim, self.camp.id, sender="stranger", expect="MessageNotSentOrReceived")
        refused(self, self.w.reclaim, self.camp.id, sender="alice", expect="MessageNotSentOrReceived")
        self.assertEqual(self.w.reclaim(self.camp.id), 970_000)

    def test_unknown_auth_mode(self):
        w = self.w
        call = w.d.reclaim(w.h, self.camp.id, ByMessage())
        call.action.solution[2] = 4
        refused(self, w.push, [call], expect=CLVM_RAISE)

    def test_creators_delegate_reclaims_paid_to_creator(self):
        w = self.w
        refused(self, w.reclaim, self.camp.id, delegate=self.hot, expect="MessageNotSentOrReceived")  # not granted
        w.set_delegate("creator", self.hot.puzzle_hash)
        hot_before = w.balance(self.hot.puzzle_hash)
        self.assertEqual(w.reclaim(self.camp.id, delegate=self.hot), 970_000)
        self.assertEqual(w.balance(self.hot.puzzle_hash), hot_before)

    def test_a_holders_delegate_is_not_the_creators(self):
        w = self.w
        w.set_delegate("alice", self.hot.puzzle_hash)
        refused(self, w.reclaim, self.camp.id, delegate=self.hot, expect="MessageNotSentOrReceived")

    def test_admin_reclaims_paid_to_creator(self):
        """FLPCR-04: the dev multisig can clear a campaign the creator left, so
        no table slot is held forever; the money still goes to the creator."""
        w = self.w
        refused(self, w.reclaim, self.camp.id, admin=True, admin_sender="stranger", expect="MessageNotSentOrReceived")
        admin_before = w.balance(w.cfg.admin_ph)
        self.assertEqual(w.reclaim(self.camp.id, admin=True), 970_000)
        self.assertEqual(w.balance(w.cfg.admin_ph), admin_before)
        self.assertIsNone(w.state().campaign(self.camp.id))


class TestAdminWaitsForTheWindow(ChainTest):
    def test_admin_cannot_reclaim_early(self):
        w = self.w
        self.created()
        _, r = self.root_at(self.engine(), self.camp.end)
        self.active(r)
        refused(self, w.reclaim, self.camp.id, admin=True, expect=CLVM_RAISE)
        w.advance_to(self.camp.end + WINDOW)
        w.reclaim(self.camp.id, admin=True)
        w.create(w.campaign("creator", 1000, w.h + LEAD, w.h + LEAD + 16, Kind.LP_HOLD, POOL, claim_window=WINDOW),
                 "creator")



class TestSpentSlotsStaySpent(ChainTest):
    """Every slot a leaf changes is spent, not just recreated. A slot left
    unspent beside its recreation is a stale record someone can prove again:
    a claim slot with a lower `claimed` pays twice, stale neighbours admit a
    second slot for one key, and a stale delegation slot outlives its revoke.
    Each probe hands the driver the stale record and tries; the build refuses
    because the slot was spent, and a mutant that drops the spend pays.

    A whale holds most of the campaign and never claims, so the posted total
    always has room for a duplicate payment: only the spent slot refuses."""

    def setUp(self):
        super().setUp()
        w = self.w
        self.hot = w.bls()
        self.src = lifecycle_src((self.alice, 1), (self.bob, 1), (w.keys.ph("whale"), 1000))
        self.created()
        self.state, self.r1 = self.root_at(self.engine(), self.camp.start + 20)
        self.active(self.r1)

    def second_root(self):
        _, r2 = self.root_at(self.state, self.camp.end)
        self.active(r2)
        return r2

    def test_a_claims_own_slot_is_spent(self):
        w, c = self.w, self.camp
        key = Distributor.claim_key(c.id, self.alice)
        w.claim(self.r1, c.id, "alice")
        stale = ClaimRecord(**vars(w.d.claims[key]))
        r2 = self.second_root()
        w.claim(r2, c.id, "alice")  # spends the slot recording r1's cumulative
        current = w.d.claims[key]
        w.d.claims[key] = stale
        try:
            refused(self, w.claim, r2, c.id, "alice", expect="Simulator error")
        finally:
            w.d.claims[key] = current

    def neighbour_of_an_insert(self, side):
        """X claims first; Y's first claim then inserts beside X, so X is Y's
        `side` neighbour and its slot is spent and recreated with a new
        pointer. X's slot as it stood before must be gone: claimed against
        again, it would pay X a second time."""
        w, c = self.w, self.camp
        keys = {who: Distributor.claim_key(c.id, w.keys.ph(who)) for who in ("alice", "bob")}
        low, high = sorted(keys, key=keys.get)
        x, y = (low, high) if side == "left" else (high, low)
        w.claim(self.r1, c.id, x)
        key = keys[x]
        stale = ClaimRecord(**vars(w.d.claims[key]))
        w.claim(self.r1, c.id, y)
        self.assertNotEqual((stale.left, stale.right), (w.d.claims[key].left, w.d.claims[key].right))
        r2 = self.second_root()
        current = w.d.claims[key]
        w.d.claims[key] = stale
        try:
            refused(self, w.claim, r2, c.id, x, expect="Simulator error")
        finally:
            w.d.claims[key] = current

    def test_the_left_neighbour_of_an_insert_is_spent(self):
        self.neighbour_of_an_insert("left")

    def test_the_right_neighbour_of_an_insert_is_spent(self):
        self.neighbour_of_an_insert("right")

    def stale_delegation_claim(self, key, stale):
        w, c = self.w, self.camp
        current = w.d.delegations.get(key)
        w.d.delegations[key] = stale
        try:
            refused(self, w.claim_delegated, self.r1, c.id, self.alice, self.hot, expect="Simulator error")
        finally:
            w.d.delegations[key] = current

    def test_a_revoke_spends_the_active_slot(self):
        w = self.w
        key = Distributor.delegation_key(self.alice, self.hot.puzzle_hash)
        w.set_delegate("alice", self.hot.puzzle_hash)
        stale = DelegationRecord(**vars(w.d.delegations[key]))  # (key, 1, 0, ...)
        w.set_delegate("alice", self.hot.puzzle_hash, grant=0)
        self.stale_delegation_claim(key, stale)

    def test_a_delegated_use_spends_the_slot(self):
        w, c = self.w, self.camp
        key = Distributor.delegation_key(self.alice, self.hot.puzzle_hash)
        w.set_delegate("alice", self.hot.puzzle_hash)
        stale = DelegationRecord(**vars(w.d.delegations[key]))  # (key, 1, 0, ...)
        w.claim_delegated(self.r1, c.id, self.alice, self.hot)  # uses 0 -> 1
        w.set_delegate("alice", self.hot.puzzle_hash, grant=0)  # (key, 0, 2, ...)
        r2 = self.second_root()
        current = w.d.delegations[key]
        w.d.delegations[key] = stale
        try:
            refused(self, w.claim_delegated, r2, c.id, self.alice, self.hot, expect="Simulator error")
        finally:
            w.d.delegations[key] = current

    def neighbour_of_a_delegation_insert(self, side):
        """The same for delegations: Alice grants X, then Y inserts beside X's
        key. X's slot as it stood before must be gone, or X could keep using
        it after Alice revokes X."""
        w, c = self.w, self.camp
        hot2 = w.bls()
        wallets = {"a": self.hot, "b": hot2}
        keys = {n: Distributor.delegation_key(self.alice, wl.puzzle_hash) for n, wl in wallets.items()}
        low, high = sorted(keys, key=keys.get)
        x, y = (low, high) if side == "left" else (high, low)
        w.set_delegate("alice", wallets[x].puzzle_hash)
        key = keys[x]
        stale = DelegationRecord(**vars(w.d.delegations[key]))
        w.set_delegate("alice", wallets[y].puzzle_hash)
        w.set_delegate("alice", wallets[x].puzzle_hash, grant=0)
        current = w.d.delegations[key]
        w.d.delegations[key] = stale
        try:
            refused(self, w.claim_delegated, self.r1, c.id, self.alice, wallets[x], expect="Simulator error")
        finally:
            w.d.delegations[key] = current

    def test_the_left_neighbour_of_a_delegation_insert_is_spent(self):
        self.neighbour_of_a_delegation_insert("left")

    def test_the_right_neighbour_of_a_delegation_insert_is_spent(self):
        self.neighbour_of_a_delegation_insert("right")


class TestMalformedHolder(ChainTest):
    """A root that really contains a 31-byte holder: only the claim leaf's
    length check refuses it before consensus would (InvalidPuzzleHash)."""

    def test_short_holder_in_the_root(self):
        from types import SimpleNamespace

        from flpcr.merkle import build_tree, claim_leaf

        w, c = self.w, self.camp
        self.created()
        short = b"\x11" * 31
        w.advance_to(c.start + 21)
        leaf = claim_leaf(w.launcher_id, c.id, short, 100)
        root, proofs = build_tree([leaf])
        r = SimpleNamespace(root_id=1, start=c.start, end=c.start + 20, root=root, totals={c.id: 100})
        w.post(r)
        self.settle()
        call = w.d.claim(w.h, c.id, short, 100, proofs[leaf])
        call.messages = []
        refused(self, w.push, [call], expect=CLVM_RAISE)



class TestDisputes(ChainTest):
    """Merkl's dispute, on coins: anyone locks a bond against a pending root;
    the guardian or admin settles it. Upheld: the root is revoked and the bond
    refunded. Rejected: the bond is forfeited to the dev multisig and the
    window restarts. A disputed root never activates on its own."""

    def setUp(self):
        super().setUp()
        self.created()
        self.state, self.r1 = self.root_at(self.engine(), self.camp.start + 20)
        self.w.post(self.r1)
        self.bond = self.w.cfg.dispute_bond

    def test_a_disputed_root_waits(self):
        w, c = self.w, self.camp
        w.dispute(1)
        self.assertEqual(w.state().dispute.root_id, 1)
        self.settle()  # past the window: still not claimable
        refused(self, w.claim, self.r1, c.id, "alice", expect=CLVM_RAISE)
        self.assertIsNotNone(w.state().pending)

    def test_upheld_revokes_and_refunds(self):
        w, c = self.w, self.camp
        before = w.xch_balance("disputer")
        w.dispute(1)
        self.assertEqual(w.xch_balance("disputer"), before + 1)  # the bond left the disputer's coins
        w.resolve(1)
        st = w.state()
        self.assertIsNone(st.pending)
        self.assertIsNone(st.dispute)
        self.assertEqual(w.xch_balance("disputer"), before + 1 + self.bond)  # refunded
        w.post(self.r1)  # the corrected root reuses the id
        self.settle()
        self.assertGreater(w.claim(self.r1, c.id, "alice"), 0)

    def test_rejected_forfeits_and_restarts_the_window(self):
        w, c = self.w, self.camp
        dev_before = w.xch_balance("dev")
        w.dispute(1)
        self.settle()
        w.resolve(0)
        st = w.state()
        self.assertIsNone(st.dispute)
        self.assertEqual(w.xch_balance("dev"), dev_before + self.bond)
        self.assertEqual(st.pending.activates_at, w.h - 1 + HEIGHT_WINDOW + w.cfg.dispute_blocks)
        refused(self, w.claim, self.r1, c.id, "alice", expect=CLVM_RAISE)  # the window restarted
        w.advance_to(st.pending.activates_at)
        self.assertGreater(w.claim(self.r1, c.id, "alice"), 0)

    def test_admin_may_resolve_too(self):
        w = self.w
        w.dispute(1)
        w.resolve(1, sender="admin")
        self.assertIsNone(w.state().pending)

    def test_veto_waits_for_resolve(self):
        w = self.w
        w.dispute(1)
        refused(self, w.veto, 1, expect=CLVM_RAISE)
        w.resolve(1)

    def test_the_bond_must_exist(self):
        w = self.w
        call = w.d.dispute(w.h, 1, w.keys.ph("disputer"), b"\x77" * 32)
        call.creations, call.extra_spends = [], []  # no launcher, no bond: nothing announces
        refused(self, w.push, [call], expect="Simulator error")

    def test_the_bond_must_be_exact(self):
        w = self.w
        refused(self, w.dispute, 1, bond=self.bond - 1, expect="Simulator error")
        refused(self, w.dispute, 1, bond=self.bond + 1, expect="Simulator error")
        w.dispute(1)

    def test_one_dispute_of_the_pending_root(self):
        w = self.w
        refused(self, w.dispute, 2, expect=CLVM_RAISE)  # not the pending root
        w.dispute(1)
        refused(self, w.dispute, 1, disputer="other", expect=CLVM_RAISE)  # already disputed
        w.resolve(1)
        refused(self, w.dispute, 1, expect=CLVM_RAISE)  # nothing pending now

    def test_refund_address_must_be_32_bytes(self):
        w = self.w
        ph = w.keys.ph("disputer")
        w.creator_coin = w.sim.new_coin(ph, self.bond + 1)
        call = w.d.dispute(w.h, 1, b"\x11" * 31, w.creator_coin.coin_id())
        refused(self, w.push, [call], expect=CLVM_RAISE)

    def test_init_checks_the_bond_hashes(self):
        """The driver derives both hashes, so a wrong one needs the raw Config
        path: the leaf, not the Python side, must refuse it."""
        for field_name, value in (("bond_puzzle_hash", b"\x01" * 31), ("bond_launcher_hash", b"\x01" * 33)):
            with self.assertRaises(ValueError) as ctx:
                SimWorld(reward_asset=self.ASSET, raw_bond={field_name: value})
            self.assertIn(CLVM_RAISE, str(ctx.exception), field_name)

    def test_cannot_dispute_an_active_root(self):
        w = self.w
        self.settle()  # the prologue promotes it on the next spend
        refused(self, w.dispute, 1, expect=CLVM_RAISE)

    def test_only_the_guardian_or_admin_resolve(self):
        w = self.w
        w.dispute(1)
        refused(self, w.resolve, 1, sender="stranger", expect=CLVM_RAISE)
        refused(self, w.resolve, 1, sender="operator", expect=CLVM_RAISE)
        call = w.d.resolve(w.h, 1)
        refused(self, w.push, [call], senders={w.cfg.guardian_ph: w.keys.ph("stranger")}, expect="MessageNotSentOrReceived")
        w.resolve(1)

    def test_resolve_needs_a_dispute_and_a_verdict(self):
        w = self.w
        with self.assertRaises(ValueError):
            w.d.resolve(w.h, 1)  # the driver refuses before the leaf does
        w.dispute(1)
        call = w.d.resolve(w.h, 1)
        call.action.solution[1] = 2
        refused(self, w.push, [call], expect=CLVM_RAISE)

    def test_the_bond_pays_only_where_the_leaf_says(self):
        """The bond coin runs exactly the delegated puzzle the singleton named."""
        w = self.w
        w.dispute(1)
        call = w.d.resolve(w.h, 1)
        st = w.state()
        thief = w.keys.ph("thief")
        bond_coin = Coin(st.dispute.bond_parent, w.d.bond_ph, self.bond)
        delegated = to_program(w.clvm, (1, [[51, thief, self.bond, [thief]]]))
        solution = to_program(w.clvm, [w.d.inner_puzzle(w.d.state).tree_hash(), delegated])
        call.extra_spends = [CoinSpend(bond_coin, w.d.bond_puzzle.serialize(), solution.serialize())]
        refused(self, w.push, [call], expect="MessageNotSentOrReceived")
        self.assertEqual(w.xch_balance("thief"), 0)


# --- the CAT twins: every class above, paying rewards in a CAT -----------------------------

for _name, _cls in list(globals().items()):
    if isinstance(_cls, type) and issubclass(_cls, ChainTest) and _cls is not ChainTest:
        globals()[_name + "Cat"] = type(_name + "Cat", (_cls,), {"ASSET": CAT_ASSET, "__module__": __name__})
del _name, _cls


if __name__ == "__main__":
    unittest.main()
