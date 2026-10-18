"""The distributor puzzles on the simulator.

Every scenario here also exists in tests/test_distributor.py against the
Python reference model, with the same numbers, so the two must agree.
Skipped when chia-wallet-sdk or the compiled puzzles are not available.
"""

import unittest

try:
    from flpcr.chain.sim import SimWorld
    from flpcr.chain.driver import Action, Distributor

    HAVE_CHAIN = True
except (ImportError, FileNotFoundError):  # pragma: no cover
    HAVE_CHAIN = False

from flpcr import EngineState, FixtureSource, HolderEvent, Kind, run_epoch

POOL, LP = b"\xb0" * 32, b"\x1b" * 32
WINDOW = 16
CAT_ASSET = b"\xa5" * 32  # a reward CAT; every test class also runs with it below


def refused(test, fn, *args, **kwargs):
    """A genuine refusal only: the leaf raised when run (`clvm raise`) or
    consensus rejected the bundle (`Simulator error`). A Python error in the
    test or driver is a bug, not a refusal, and fails the test."""
    with test.assertRaises(ValueError) as ctx:
        fn(*args, **kwargs)
    message = str(ctx.exception)
    test.assertTrue("clvm raise" in message or "Simulator error" in message, message)


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class ChainTest(unittest.TestCase):
    ASSET = bytes(32)  # XCH

    def setUp(self):
        self.w = SimWorld(reward_asset=self.ASSET)
        w = self.w
        w.advance_to(w.h + 3)
        self.camp = w.campaign("creator", 1_000_000, w.h + 2, w.h + 42, Kind.LP_HOLD, POOL, claim_window=WINDOW)
        self.alice, self.bob = w.keys.ph("alice"), w.keys.ph("bob")
        self.src = FixtureSource(
            lp_assets={POOL: LP},
            events={LP: [HolderEvent(0, self.alice, 3), HolderEvent(0, self.bob, 1)]},
        )

    def created(self):
        self.w.create(self.camp, "creator")

    def root_at(self, state, end):
        self.w.advance_to(end + 1)
        return run_epoch(state, [self.camp], self.src, end)

    def active(self, root):
        self.w.post(root)
        self.w.advance_to(self.w.h + self.w.cfg.dispute_blocks)


class TestLifecycle(ChainTest):
    def test_fee_claims_over_time_and_leftovers_to_creator(self):
        w, c = self.w, self.camp
        self.created()
        st = w.d.read_state()
        self.assertEqual((st["reserve"], st["fees_owed"]), (1_000_000, 30_000))
        self.assertEqual(st["campaigns"][c.id]["budget"], 970_000)
        self.assertEqual(w.reserve_on_chain(), 1_000_000)

        state, r1 = self.root_at(EngineState(w.launcher_id, height=c.start), c.start + 20)
        w.post(r1)
        refused(self, w.claim, r1, c.id, "alice")  # still in its dispute window
        w.advance_to(w.h + w.cfg.dispute_blocks)
        self.assertEqual(w.claim(r1, c.id, "alice"), 363_750)
        refused(self, w.claim, r1, c.id, "alice")  # nothing new

        state, r2 = self.root_at(state, c.end)
        self.active(r2)
        self.assertEqual(w.claim(r2, c.id, "alice"), 363_750)

        refused(self, w.reclaim, c.id)  # window still open
        w.advance_to(c.end + WINDOW)
        self.assertEqual(w.reclaim(c.id), 242_500)  # Bob never claimed
        refused(self, w.claim, r2, c.id, "bob")  # reclaimed: nothing left to claim from
        self.assertEqual(w.collect(), 30_000)
        self.assertEqual(w.d.read_state()["reserve"], 0)
        self.assertEqual(w.reserve_on_chain(), 0)

    def test_late_claim_before_the_creator_reclaims(self):
        w, c = self.w, self.camp
        self.created()
        _, r = self.root_at(EngineState(w.launcher_id, height=c.start), c.end)
        self.active(r)
        w.advance_to(c.end + 2 * WINDOW)  # window long past, creator has not reclaimed
        self.assertEqual(w.claim(r, c.id, "bob"), 242_500)
        self.assertEqual(w.reclaim(c.id), 727_500)  # only Alice's share was left
        refused(self, w.claim, r, c.id, "alice")

    def test_reserve_matches_state_after_every_step(self):
        w, c = self.w, self.camp
        self.created()
        state, r1 = self.root_at(EngineState(w.launcher_id, height=c.start), c.start + 10)
        self.active(r1)
        for holder in ("bob", "alice"):
            w.claim(r1, c.id, holder)
            st = w.d.read_state()
            owed = sum(x["budget"] - x["claimed"] for x in st["campaigns"].values())
            self.assertEqual(st["reserve"], owed + st["fees_owed"])
            self.assertEqual(w.reserve_on_chain(), st["reserve"])

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
        c = w.campaign("creator", 1_000_000, w.h + 2, w.h + 42, Kind.LP_HOLD, POOL, claim_window=WINDOW,
                       params=params)
        carol = w.keys.ph("carol")
        src = FixtureSource(
            lp_assets={POOL: LP},
            events={LP: [HolderEvent(0, self.alice, 1), HolderEvent(0, self.bob, 1), HolderEvent(0, carol, 1)],
                    spell: [HolderEvent(0, self.alice, 500)]},
            nfts={wizards: [HolderEvent(0, self.alice, 1), HolderEvent(0, self.bob, 1)]},
        )
        w.create(c, "creator")
        w.advance_to(c.end + 1)
        _, r = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src, c.end)
        self.active(r)
        self.assertEqual(w.claim(r, c.id, "alice"), 646_666)  # 2x boost: 2/3 of 970,000
        self.assertEqual(w.claim(r, c.id, "bob"), 323_333)
        self.assertIsNone(r.claim_for(c.id, carol))  # no Wizard, no leaf

    def test_several_actions_in_one_spend(self):
        w, c = self.w, self.camp
        self.created()
        _, r = self.root_at(EngineState(w.launcher_id, height=c.start), c.end)
        self.active(r)
        action, updates = w.claim_action(r, c.id, self.alice)
        w.push([action, Action("collect", [w.h])], slot_updates=updates)
        self.assertEqual(w.balance("alice"), 727_500)
        self.assertEqual(w.balance("treasury"), 30_000)


class TestCreateRefusals(ChainTest):
    def test_needs_the_routers_message(self):
        refused(self, self.w.create, self.camp, "creator", router="stranger")

    def test_needs_the_deposit(self):
        # Conservation is over the whole bundle: the reserve is always funded in
        # full, by someone. With no slack in the bundle, one mojo short fails.
        refused(self, self.w.create, self.camp, "creator", deposit=self.camp.amount - 1)
        self.w.create(self.camp, "creator", deposit=self.camp.amount)
        self.assertEqual(self.w.reserve_on_chain(), self.camp.amount)

    def test_campaign_length_bounds(self):
        w = self.w
        short = w.campaign("creator", 1000, w.h + 2, w.h + 2 + 15, Kind.LP_HOLD, POOL, claim_window=WINDOW)
        refused(self, w.create, short, "creator")
        ok = w.campaign("creator", 1000, w.h + 2, w.h + 2 + 16, Kind.LP_HOLD, POOL, claim_window=WINDOW)
        w.create(ok, "creator")

    def test_claim_window_floor_and_past_start(self):
        w = self.w
        refused(self, w.create, w.campaign("creator", 1000, w.h + 2, w.h + 40, Kind.LP_HOLD, POOL, claim_window=15), "creator")
        refused(self, w.create, w.campaign("creator", 1000, w.h - 1, w.h + 40, Kind.LP_HOLD, POOL, claim_window=WINDOW), "creator")

    def test_duplicate_campaign(self):
        self.created()
        refused(self, self.w.create, self.camp, "creator")

    def test_init_runs_once(self):
        refused(self, self.w.push, [Action("init", [self.w.h])])


class TestRootRefusals(ChainTest):
    def setUp(self):
        super().setUp()
        self.created()
        self.state, self.r1 = self.root_at(EngineState(self.w.launcher_id, height=self.camp.start), self.camp.start + 20)

    def test_only_the_operator_posts(self):
        refused(self, self.w.post, self.r1, sender="stranger")

    def test_emission_ceiling(self):
        c = self.camp
        refused(self, self.w.post, self.r1, totals={c.id: c.emittable(self.r1.end) + 1})
        self.w.post(self.r1, totals={c.id: c.emittable(self.r1.end)})

    def test_one_pending_at_a_time_and_sequence(self):
        w = self.w
        w.post(self.r1)
        _, r2 = run_epoch(self.state, [self.camp], self.src, self.camp.start + 25)
        w.advance_to(r2.end + 1)
        refused(self, w.post, r2)  # r1 still in its window
        w.advance_to(w.h + w.cfg.dispute_blocks)
        refused(self, w.post, r2, root_id=3)
        w.post(r2)

    def test_totals_never_fall(self):
        w, c = self.w, self.camp
        self.active(self.r1)
        _, r2 = self.root_at(self.state, c.start + 30)
        refused(self, w.post, r2, totals={c.id: self.r1.totals[c.id] - 1})

    def test_guardian_veto(self):
        w, c = self.w, self.camp
        w.post(self.r1)
        refused(self, w.veto, 1, sender="stranger")
        w.veto(1)
        w.advance_to(w.h + w.cfg.dispute_blocks)
        refused(self, w.claim, self.r1, c.id, "alice")  # never became active
        w.post(self.r1)  # corrected root, same id
        w.advance_to(w.h + w.cfg.dispute_blocks)
        refused(self, w.veto, 1)  # too late once active
        self.assertGreater(w.claim(self.r1, c.id, "alice"), 0)

    def test_rotate_operator(self):
        w = self.w
        refused(self, w.rotate, "engine2", sender="stranger")
        w.rotate("engine2")
        refused(self, w.post, self.r1)
        w.post(self.r1, sender="engine2")


class TestClaimRefusals(ChainTest):
    def setUp(self):
        super().setUp()
        self.created()
        _, self.r = self.root_at(EngineState(self.w.launcher_id, height=self.camp.start), self.camp.end)
        self.active(self.r)

    def test_forged_amount_and_holder(self):
        w, c = self.w, self.camp
        cl = self.r.claim_for(c.id, self.bob)
        refused(self, w.claim, self.r, c.id, "bob", cumulative=cl.cumulative + 1)
        stranger = w.keys.ph("stranger")
        action, updates = w.claim_action(self.r, c.id, self.bob)
        action.solution[2] = stranger  # Bob's proof, paid to someone else
        refused(self, w.push, [action], slot_updates=updates)

    def test_first_claim_cannot_insert_a_second_slot(self):
        w, c = self.w, self.camp
        w.claim(self.r, c.id, "alice")
        # Pretend Alice has no slot yet: an insert between her real neighbours.
        key = Distributor.claim_key(c.id, self.alice)
        saved = w.d.slots.pop(key)
        try:
            refused(self, w.claim, self.r, c.id, "alice")
        finally:
            w.d.slots[key] = saved

    def test_claim_is_paid_once_per_cumulative(self):
        w, c = self.w, self.camp
        self.assertEqual(w.claim(self.r, c.id, "bob"), 242_500)
        refused(self, w.claim, self.r, c.id, "bob")


class TestAssertionProbes(ChainTest):
    """One probe per assertion the mutation run (scripts/mutate.py) found
    unpinned. Each is built so that the assertion it names is the first thing
    that can refuse it."""

    PARAMS = [10000, 0, 0, None, None, 1]

    def raw(self, **kw):
        w = self.w
        f = dict(creator="creator", amount=1000, start=w.h + 2, end=w.h + 40, kind=1, target=POOL,
                 params=list(self.PARAMS), nonce=0, claim_window=WINDOW)
        f.update(kw)
        return w.create_raw(**f)

    # --- create ---

    def test_create_baseline_is_accepted(self):
        self.raw()

    def test_create_amount_and_budget(self):
        refused(self, self.raw, amount=0)
        refused(self, self.raw, amount=-1000, deposit=0)

    def test_create_max_length(self):
        w = self.w
        refused(self, self.raw, end=w.h + 2 + w.cfg.max_campaign_length + 1)

    def test_create_kind(self):
        refused(self, self.raw, kind=0)
        refused(self, self.raw, kind=3)

    def test_create_params(self):
        refused(self, self.raw, params=[10001, -1, 0, None, None, 1])
        refused(self, self.raw, params=[-1, 10001, 0, None, None, 1])
        refused(self, self.raw, params=[9999, 0, 0, None, None, 1])
        refused(self, self.raw, kind=2, params=[5000, 5000, 0, None, None, 1])
        self.raw(kind=1, params=[5000, 5000, 0, None, None, 1])

    def test_create_claim_window_ceiling(self):
        refused(self, self.raw, claim_window=self.w.cfg.max_campaign_length + 1)

    def test_create_campaign_cap(self):
        w = SimWorld(reward_asset=self.ASSET, max_campaigns=1)
        w.create_raw("creator", 1000, w.h + 2, w.h + 40, 1, POOL, list(self.PARAMS), 0, WINDOW)
        refused(self, w.create_raw, "creator", 1000, w.h + 2, w.h + 40, 1, POOL, list(self.PARAMS), 1, WINDOW)

    # --- the height pin ---

    def test_height_must_be_positive(self):
        self.assertLess(self.w.h, 32)  # below 32, ASSERT_BEFORE_HEIGHT h+32 cannot catch h = 0
        refused(self, self.raw, h=0)

    def test_one_height_per_spend(self):
        w, c = self.w, self.camp
        self.created()
        _, r = self.root_at(EngineState(w.launcher_id, height=c.start), c.end)
        self.active(r)
        action, updates = w.claim_action(r, c.id, self.bob)
        early = Action("reclaim", [w.h + c.claim_window + 100, c.id])  # a later action claiming a later height
        refused(self, w.push, [action, early], slot_updates=updates)

    # --- post_root ---

    def posted_setup(self):
        self.created()
        return self.root_at(EngineState(self.w.launcher_id, height=self.camp.start), self.camp.start + 20)

    def test_post_cannot_replace_a_pending_root(self):
        w, c = self.w, self.camp
        state, r1 = self.posted_setup()
        w.post(r1)
        refused(self, w.post, r1, totals={c.id: 1})  # same id, different totals, still pending

    def test_post_window_and_future(self):
        w, c = self.w, self.camp
        state, r1 = self.posted_setup()
        refused(self, w.post, r1, start=r1.end, end=r1.end)
        future = w.h + 50
        refused(self, w.post, r1, end=future, totals={c.id: c.emittable(future)})

    def test_post_first_id_and_tiling(self):
        w, c = self.w, self.camp
        state, r1 = self.posted_setup()
        refused(self, w.post, r1, root_id=2)
        self.active(r1)
        _, r2 = self.root_at(state, c.start + 30)
        refused(self, w.post, r2, start=r1.end + 1)
        refused(self, w.post, r2, start=r1.end - 1)
        w.post(r2)

    def test_post_totals_sorted_unique_and_kept(self):
        w, c = self.w, self.camp
        state, r1 = self.posted_setup()
        t = r1.totals[c.id]
        refused(self, w.post, r1, totals_list=[(c.id, t), (c.id, t)])
        self.active(r1)
        _, r2 = self.root_at(state, c.start + 30)
        refused(self, w.post, r2, totals_list=[])  # a live campaign may not be dropped

    def test_post_unknown_campaign(self):
        w = self.w
        state, r1 = self.posted_setup()
        refused(self, w.post, r1, totals_list=[(b"\x77" * 32, 1)])

    # --- veto ---

    def test_veto_names_the_pending_root(self):
        w = self.w
        state, r1 = self.posted_setup()
        w.post(r1)
        refused(self, w.veto, 2)
        w.veto(1)

    # --- claim ---

    def test_claims_cannot_exceed_the_posted_total(self):
        w, c = self.w, self.camp
        self.created()
        _, r = self.root_at(EngineState(w.launcher_id, height=c.start), c.end)
        w.post(r, totals={c.id: r.totals[c.id] - 1})  # leaves sum to one more than the stated total
        w.advance_to(w.h + w.cfg.dispute_blocks)
        w.claim(r, c.id, "alice")
        refused(self, w.claim, r, c.id, "bob")

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
        _, r = self.root_at(EngineState(w.launcher_id, height=c.start), c.end)
        self.active(r)
        for holder in ["alice", *small]:
            w.claim(r, c.id, holder)
        d = w.d
        keys = sorted(d.slots)
        cl = r.claim_for(c.id, self.alice)
        sides = set()
        for lk, rk in zip(keys, keys[1:]):
            if lk <= alice_key <= rk:
                continue
            sides.add("left" if rk < alice_key else "right")
            L, R = d.slots[lk], d.slots[rk]
            info = [lk, L.claimed, L.left, rk, R.claimed, R.right]
            sol = [w.h, c.id, self.alice, cl.cumulative, cl.proof, 1, info]
            refused(self, w.push, [Action("claim", sol)])
        self.assertEqual(sides, {"left", "right"})


class TestLifecycleCat(TestLifecycle):
    ASSET = CAT_ASSET


class TestCreateRefusalsCat(TestCreateRefusals):
    ASSET = CAT_ASSET


class TestRootRefusalsCat(TestRootRefusals):
    ASSET = CAT_ASSET


class TestClaimRefusalsCat(TestClaimRefusals):
    ASSET = CAT_ASSET


if __name__ == "__main__":
    unittest.main()
