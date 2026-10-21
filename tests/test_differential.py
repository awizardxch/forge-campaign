"""Random scenarios through the reference model and the puzzles side by side.

Each step picks an operation (post a root, claim for some holder, reclaim,
the dev fee is checked at creation) and applies it to both `flpcr.distributor.Distributor` and the
simulator. Both must pay the same amount or both must refuse.
"""

import random
import unittest

try:
    from flpcr.chain.sim import SimWorld

    HAVE_CHAIN = True
except (ImportError, FileNotFoundError):  # pragma: no cover
    HAVE_CHAIN = False

from flpcr import Config, Distributor, EngineState, FixtureSource, HolderEvent, Kind, Refused, run_epoch

POOL, LP, CAT = b"\xb0" * 32, b"\x1b" * 32, b"\x5c" * 32
HOLDERS = ["h1", "h2", "h3", "h4", "h5"]


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestModelAndChainAgree(unittest.TestCase):
    def run_scenario(self, seed: int):
        rng = random.Random(seed)
        # Odd seeds pay rewards in a CAT, even seeds in XCH.
        w = SimWorld(reward_asset=b"\xa5" * 32 if seed % 2 else bytes(32))
        k = w.keys
        model = Distributor(
            Config(w.launcher_id, w.cfg.reward_asset, w.cfg.dev_fee_ph, w.cfg.guardian_ph, w.cfg.router_ph,
                   fee_bps=w.cfg.fee_bps, dispute_blocks=w.cfg.dispute_blocks,
                   min_campaign_length=w.cfg.min_campaign_length,
                   max_campaign_length=w.cfg.max_campaign_length),
            w.cfg.engine_ph,
        )
        w.advance_to(w.h + 2)
        start = w.h + 2
        campaigns = [
            w.campaign("c1", rng.randint(10**5, 10**7), start, start + rng.randint(30, 60), Kind.LP_HOLD, POOL,
                       claim_window=rng.randint(16, 40)),
            w.campaign("c2", rng.randint(10**5, 10**7), start + 5, start + rng.randint(40, 70), Kind.CAT_HOLD, CAT,
                       claim_window=rng.randint(16, 40)),
        ]
        for c, creator in zip(campaigns, ("c1", "c2")):
            model.create(w.h, w.cfg.router_ph, c, c.amount)
            w.create(c, creator)
        self.assertEqual(w.balance("dev"), model.dev_fee_paid)  # paid at creation, nothing to collect

        events = {LP: [], CAT: []}
        for asset in (LP, CAT):
            bal = {hd: 0 for hd in HOLDERS}
            for height in sorted(rng.sample(range(0, start + 70), 25)):
                hd = rng.choice(HOLDERS)
                delta = rng.randint(-bal[hd], 500)
                if delta:
                    bal[hd] += delta
                    events[asset].append(HolderEvent(height, k.ph(hd), delta))
        src = FixtureSource(lp_assets={POOL: LP}, events=events)

        engine = EngineState(w.launcher_id, height=start)
        roots = []
        self.stats = {"paid": 0, "refused": 0, "reclaimed": 0}
        last_post = 0
        end_of_all = max(c.end + c.claim_window for c in campaigns) + 20
        while w.h < end_of_all:
            w.advance_to(w.h + rng.randint(1, 6))
            h = w.h
            live = [c for c in campaigns if c.id in model.campaigns]
            op = rng.random()
            if op < 0.35 and live and h > engine.height and h - last_post > w.cfg.dispute_blocks:
                new_engine, root = run_epoch(engine, live, src, h)
                if root is None or root.root is None:  # nothing accrued: run_epoch posts nothing (G-6)
                    continue
                model.post_root(h, w.cfg.engine_ph, root.root_id, root.start, root.end, root.root, root.totals)
                w.post(root)
                engine, last_post = new_engine, h
                roots.append(root)
            elif op < 0.8 and roots:
                c = rng.choice(campaigns)
                hd = rng.choice(HOLDERS)
                root = model.active and next((r for r in roots if r.root_id == model.active.root_id), None)
                if root is None or root.claim_for(c.id, k.ph(hd)) is None:
                    continue
                cl = root.claim_for(c.id, k.ph(hd))
                try:
                    expected = model.claim(h, c.id, k.ph(hd), cl.cumulative, cl.proof, k.ph(hd))[1]
                except Refused:
                    expected = None
                try:
                    got = w.claim(root, c.id, hd)
                except ValueError:  # a refusal; any other error is a bug
                    got = None
                self.assertEqual(got, expected, f"seed {seed} claim {hd} at {h}")
                self.stats["paid" if got else "refused"] += 1
            else:
                c = rng.choice(campaigns)
                try:
                    expected = model.reclaim(h, c.id, c.creator)[1]
                except Refused:
                    expected = None
                try:
                    got = w.reclaim(c.id) if c.id in w.d.read_state()["campaigns"] else None
                except ValueError:  # a refusal; any other error is a bug
                    got = None
                self.assertEqual(got, expected, f"seed {seed} reclaim at {h}")
                self.stats["reclaimed"] += got is not None
            st = w.d.read_state()
            self.assertEqual(st["reserve"], model.reserve)
            self.assertEqual(w.reserve_on_chain(), model.reserve)
            model.check_invariant()
        return len(roots)

    def test_random_scenarios(self):
        totals = {"roots": 0, "paid": 0, "refused": 0, "reclaimed": 0}
        for seed in range(12):
            totals["roots"] += self.run_scenario(seed)
            for key, n in self.stats.items():
                totals[key] += n
        # The scenarios must actually exercise every path, both ways.
        for key, n in totals.items():
            self.assertGreater(n, 0, f"no {key} in any scenario: {totals}")
        print(totals)


if __name__ == "__main__":
    unittest.main()
