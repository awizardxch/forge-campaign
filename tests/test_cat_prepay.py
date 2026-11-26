"""CAT rewards: the 3% Forge fee comes out of the CAT deposit; the network fee and the root posts are
XCH. CAT rewards prepay their roots too (owner, 2026-11-26: "make sure this works for cat rewards as
well"). The deposit is the CAT; the operator's root posts are paid in XCH from the creator's
consent coin, beside the network fee, on every path a CAT campaign is created by:

* a create on the CAT's hosted distributor (flpcr.service.intake);
* the first campaign of a CAT with no distributor yet, which launches it (flpcr.service.launch);

and on testnet the posting fee is a token amount (FLPCR_POST_FEE, compose.yaml).
"""
import unittest

from test_intake import ASSET, HAVE_CHAIN, IntakeWorld
from flpcr.router import EXTRA_POSTS, RouterRules, XCH

POST = 1_000_000  # testnet's FLPCR_POST_FEE


def received(w, ph, amount):
    return sum(c.amount for c in w.sim.unspent_coins(ph, False) if c.amount == amount)


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestCatCreatePrepays(unittest.TestCase):
    def setUp(self):
        self.x = IntakeWorld(reward_asset=ASSET, rules=RouterRules.permissive(post_fee=POST))

    def test_a_cat_create_pays_the_operator_in_xch(self):
        x = self.x
        q = x.intake.quote(x.request())  # 40 blocks, every 10: 4 posts + 2
        want = POST * (4 + EXTRA_POSTS)
        self.assertEqual(q["operator_fee"], want)
        self.assertEqual(q["conditions"]["operator_payment"]["puzzle_hash"], x.op.puzzle_hash.hex())
        with self.assertRaisesRegex(ValueError, "pay the operator 0 mojos"):
            x.intake.create({"quote": q["quote"], "spend_bundle": x.cat_bundle(q, pay=0)})
        with self.assertRaisesRegex(ValueError, f"pay the operator {want - 1:,} mojos"):
            x.intake.create({"quote": q["quote"], "spend_bundle": x.cat_bundle(q, pay=want - 1)})
        dev_cat, dev_xch = x.w.balance("dev"), x.w.xch_balance("dev")
        out = x.intake.create({"quote": q["quote"], "spend_bundle": x.cat_bundle(q)})
        self.assertEqual(out["status"], "created")
        # Owner, 2026-11-26: the network fee is XCH, the campaign percentage is the CAT.
        self.assertEqual(x.w.balance("dev") - dev_cat, 30_000, "the 3% Forge fee is paid in the reward CAT")
        self.assertEqual(x.w.xch_balance("dev") - dev_xch, 0, "no part of the 3% is taken in XCH")
        self.assertGreater(q["network_fee"], 0, "the network fee is XCH, from the consent coin (RESERVE_FEE)")
        self.assertEqual(received(x.w, x.op.puzzle_hash, want), want, "the operator got the posts in XCH")
        self.assertEqual(x.svc.d.reserve_coin.amount, 970_000, "the CAT deposit is the campaign's, whole")
        self.assertEqual(x.w.balance(x.creator.puzzle_hash), 500, "the CAT change came back")


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestLaunchPrepays(unittest.TestCase):
    """The first campaign of an asset launches its distributor and prepays its roots, CAT or XCH."""

    def setUp(self):
        from test_fleet import FleetWorld
        self.fx = FleetWorld(rules=RouterRules.permissive(post_fee=POST))

    def check(self, asset):
        fx = self.fx
        creator = fx.creator()
        q, funding, seed = fx.launch_quote(creator, asset)
        self.assertGreater(q["operator_fee"], 0)
        want = q["operator_fee"]
        self.assertEqual(want % POST, 0)
        op = bytes.fromhex(q["conditions"]["operator_payment"]["puzzle_hash"])
        self.assertEqual(op, fx.op.puzzle_hash)
        with self.assertRaisesRegex(ValueError, "pay the operator 0 mojos"):
            fx.fleet.launches.create(fx.launch_bundle(creator, q, funding, seed, pay=0), wait=5, poll=0)
        q, funding, seed = fx.launch_quote(creator, asset)
        out = fx.fleet.launches.create(fx.launch_bundle(creator, q, funding, seed), wait=5, poll=0)
        self.assertEqual(out["status"], "created", out)
        self.assertEqual(received(fx.w, fx.op.puzzle_hash, q["operator_fee"]), q["operator_fee"])

    def test_a_cat_launch(self):
        from test_fleet import ASSET_A
        self.check(ASSET_A)


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestCatCreateOnAFleet(unittest.TestCase):
    def test_a_second_cat_campaign_on_the_launched_distributor_prepays(self):
        from test_fleet import ASSET_A, FleetWorld
        fx = FleetWorld(rules=RouterRules.permissive(post_fee=POST))
        creator = fx.creator()
        fx.launch(creator, ASSET_A)
        intake = fx.fleet.for_asset(ASSET_A).intake
        q = intake.quote(fx.request(creator, ASSET_A))
        want = q["operator_fee"]
        self.assertGreater(want, 0)
        with self.assertRaisesRegex(ValueError, "pay the operator 0 mojos"):
            intake.create(fx.cat_create_bundle(creator, q, ASSET_A, pay=0), wait=5, poll=0)
        q = intake.quote(fx.request(creator, ASSET_A))
        out = intake.create(fx.cat_create_bundle(creator, q, ASSET_A), wait=5, poll=0)
        self.assertEqual(out["status"], "created")


class TestTestnetPostFee(unittest.TestCase):
    def test_flpcr_post_fee_overrides_the_rules(self):
        from flpcr.service.main import rules_from_env
        self.assertEqual(rules_from_env("").post_fee, RouterRules().post_fee)
        self.assertEqual(rules_from_env("", "1000000").post_fee, 1_000_000)
        self.assertEqual(rules_from_env('{"post_fee": 5}', "1000000").post_fee, 1_000_000)
        self.assertEqual(rules_from_env('{"post_fee": 5}', "").post_fee, 5)
        with self.assertRaises(ValueError):
            rules_from_env("", "-1")

    def test_the_testnet_host_sets_it(self):
        import pathlib
        compose = pathlib.Path(__file__).resolve().parents[2] / "chia-cfmm" / "deploy" / "hetzner" / "compose.yaml"
        if not compose.exists():
            self.skipTest("the Hetzner compose file is not beside this repository")
        text = compose.read_text(encoding="utf-8")
        self.assertIn("FLPCR_POST_FEE: ${FLPCR_TESTNET_POST_FEE:-1000000}", text)
        mainnet = text[text.index("  flpcr-mainnet:"):]
        self.assertNotIn("FLPCR_POST_FEE", mainnet.split("\n  caddy:")[0], "mainnet keeps the default")


if __name__ == "__main__":
    unittest.main()
