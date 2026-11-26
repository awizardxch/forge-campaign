"""Mainnet testing rules (owner, 2026-11-25), on the simulator:

* the guardian veto a 🔐 lock proposes: the service builds the distributor's keyless half, holds its
  own writers off the coin the lock's message names while the owners sign, and the veto lands
  beside the guardian's message alone;
* the creation gate: only the wallets the Forge router approves for deployment (or, when it is
  NFT-gated, the holders of its collections) may create, read from the router's own gate;
* every create prepays its campaign's root posts to the operator, as a pool's creator pays for
  its whole creation.
"""

import unittest

from test_intake import HAVE_CHAIN, IntakeWorld

from flpcr.router import EXTRA_POSTS, RouterRules, operator_fee

if HAVE_CHAIN:
    from chia_wallet_sdk import Clvm, Signature

    from flpcr.chain.values import to_program
    from flpcr.service.chain import bundle_json
    from flpcr.service.intake import router_create_gate
    from flpcr.service.lease import LeaseBusy
    from flpcr.service.sequencer import ClaimSequencer, Pending


def coin_spends(raw):
    """CoinSpends from the JSON the service answers with."""
    from chia_wallet_sdk import Coin, CoinSpend
    h = lambda x: bytes.fromhex(str(x).removeprefix("0x"))  # noqa: E731
    return [CoinSpend(Coin(h(s["coin"]["parent_coin_info"]), h(s["coin"]["puzzle_hash"]), int(s["coin"]["amount"])),
                      h(s["puzzle_reveal"]), h(s["solution"])) for s in raw]


class TestOperatorFee(unittest.TestCase):
    def test_one_post_per_every_blocks_plus_the_final_and_a_repost(self):
        r = RouterRules(post_fee=2_000_000_000)
        self.assertEqual(operator_fee(r, 180, 180), 2_000_000_000 * (1 + EXTRA_POSTS))
        self.assertEqual(operator_fee(r, 181, 180), 2_000_000_000 * (2 + EXTRA_POSTS))
        self.assertEqual(operator_fee(r, 7 * 4608, 180), 2_000_000_000 * (180 + EXTRA_POSTS))  # 7 days: 0.364 XCH
        self.assertEqual(operator_fee(RouterRules(post_fee=0), 4608, 180), 0)
        self.assertEqual(RouterRules.permissive().post_fee, 0)


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestOperatorPrepay(unittest.TestCase):
    def setUp(self):
        self.x = IntakeWorld(rules=RouterRules.permissive(post_fee=1_000_000))

    def bundle(self, q, pay):
        """The creator's XCH coin as IntakeWorld builds it, paying the operator `pay` mojos."""
        x = self.x
        w = x.w
        amount, fee = q["fields"]["amount"], q["network_fee"]
        op = bytes.fromhex(q["conditions"]["operator_payment"]["puzzle_hash"])
        coin = w.sim.new_coin(x.creator.puzzle_hash, amount + fee + pay + 5_000_000)
        msg = bytes.fromhex(q["conditions"]["message"]["message"])
        clvm = Clvm()
        conds = [[66, 23, msg, bytes.fromhex(q["singleton"]["coin_id"])],
                 [51, x.creator.puzzle_hash, coin.amount - amount - fee - pay], [52, fee]]
        if pay:
            conds.append([51, op, pay])
        clvm.spend_standard_coin(coin, x.creator.pk, clvm.delegated_spend([to_program(clvm, c) for c in conds]))
        spends = list(clvm.coin_spends())
        return bundle_json(spends, x.sign(spends))

    def test_a_create_pays_the_operator_for_its_roots(self):
        x = self.x
        q = x.intake.quote(x.request())  # 40 blocks, every 10: 4 posts + 2
        want = 1_000_000 * (4 + EXTRA_POSTS)
        self.assertEqual(q["operator_fee"], want)
        pay = q["conditions"]["operator_payment"]
        self.assertEqual((pay["puzzle_hash"], pay["amount"]), (x.op.puzzle_hash.hex(), want))
        with self.assertRaisesRegex(ValueError, "pay the operator 0 mojos"):
            x.intake.create({"quote": q["quote"], "spend_bundle": self.bundle(q, 0)})
        with self.assertRaisesRegex(ValueError, f"pay the operator {want - 1:,} mojos"):
            x.intake.create({"quote": q["quote"], "spend_bundle": self.bundle(q, want - 1)})
        before = x.w.xch_balance(x.op.puzzle_hash) if hasattr(x.w, "xch_balance") else None
        out = x.intake.create({"quote": q["quote"], "spend_bundle": self.bundle(q, want)})
        self.assertEqual(out["status"], "created")
        got = sum(c.amount for c in x.w.sim.unspent_coins(x.op.puzzle_hash, False) if c.amount == want)
        self.assertEqual(got, want, "the operator's address received the prepaid posts")
        self.assertEqual(x.svc.d.reserve_coin.amount, 970_000, "the deposit is the campaign's, not the posts'")
        del before


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestCreationGate(unittest.TestCase):
    def probe(self, allowed=None, mode="list", collections=(), fail=False):
        calls = []

        def fetch(url):
            calls.append(url)
            if fail:
                raise OSError("down")
            return {"success": True, "gated": True, "mode": mode, "collections": list(collections),
                    "allowed": allowed(url) if callable(allowed) else bool(allowed)}
        return fetch, calls

    def test_the_routers_gate_admits_and_refuses_and_fails_closed(self):
        ok, calls = self.probe(allowed=True)
        gate = router_create_gate("https://router.test", "mainnet", fetch=ok)
        self.assertEqual(gate(b"\x11" * 32), (True, gate(b"\x11" * 32)[1]))
        self.assertEqual(len(calls), 1, "kept for the ttl")
        self.assertIn("/api/forge-create?probe=gate&address=xch1", calls[0])
        no, _ = self.probe(allowed=False)
        allowed, why = router_create_gate("https://router.test", "mainnet", fetch=no)(b"\x11" * 32)
        self.assertFalse(allowed)
        self.assertIn("wallets the Forge approved", why)
        nft, _ = self.probe(allowed=False, collections=[{"id": "col1abc", "label": "aWizard"}])
        self.assertIn("an NFT from aWizard", router_create_gate("https://r", "mainnet", fetch=nft)(b"\x11" * 32)[1])
        down, calls = self.probe(fail=True)
        gate = router_create_gate("https://r", "mainnet", fetch=down)
        self.assertFalse(gate(b"\x11" * 32)[0])
        self.assertFalse(gate(b"\x11" * 32)[0])
        self.assertEqual(len(calls), 2, "a failed read is not remembered")

    def test_quote_and_create_follow_the_gate(self):
        x = IntakeWorld()
        approved = {x.creator.puzzle_hash}
        x.intake.create_gate = lambda ph: (ph in approved, "campaign creation is limited to wallets the Forge approved")
        q = x.intake.quote(x.request())
        approved.clear()  # the operator takes the wallet off the list before it signs
        with self.assertRaisesRegex(ValueError, "limited to wallets the Forge approved"):
            x.intake.create({"quote": q["quote"], "spend_bundle": x.xch_bundle(q)})
        with self.assertRaisesRegex(ValueError, "not routable: campaign creation is limited"):
            x.intake.quote(x.request())
        approved.add(x.creator.puzzle_hash)
        q = x.intake.quote(x.request())
        self.assertEqual(x.intake.create({"quote": q["quote"], "spend_bundle": x.xch_bundle(q)})["status"], "created")
        self.assertTrue(x.intake.terms()["create_gate"]["gated"])


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestGuardianVeto(unittest.TestCase):
    def setUp(self):
        x = self.x = IntakeWorld()
        q = x.intake.quote(x.request())
        x.intake.create({"quote": q["quote"], "spend_bundle": x.xch_bundle(q)})
        x.w.advance_to(q["fields"]["start"] + 12)
        self.posted = x.svc.operator_step(wait=5, poll=0)
        self.guardian = x.svc.d.cfg.guardian_ph

    def guardian_spend(self, half):
        """The guardian's coin carrying the one condition the half names (a 🔐 lock's spend does the
        same from its vault)."""
        w = self.x.w
        cond = half["condition"]
        coin = w.sim.new_coin(self.guardian, 1)
        return w.keys.spend(coin, w.keys.label_of(self.guardian),
                            [[cond[0], cond[1], bytes.fromhex(cond[2][2:]), bytes.fromhex(cond[3][2:])]])

    def test_the_veto_lands_beside_the_guardians_message_alone(self):
        x = self.x
        rid = self.posted["root_id"]
        self.assertEqual(x.svc.d.chain_state.pending.root_id, rid)
        half = x.svc.guardian_veto(rid, self.guardian, hold=True)
        self.assertEqual(half["coin_id"], x.svc.d.coin.coin_id().hex())
        self.assertEqual(half["condition"][:2], [66, 23])
        spends = coin_spends(half["spends"])
        with self.assertRaises(Exception):  # the distributor's half alone: no message, refused
            x.svc.chain.push(spends, Signature.infinity())
        x.svc.chain.push(spends + [self.guardian_spend(half)], Signature.infinity())
        x.svc.follow()
        self.assertIsNone(x.svc.d.chain_state.pending, "the pending root is cleared")
        self.assertIsNone(x.svc.lease.guardian(), "the hold ends once the coin it named is spent")

    def test_the_hold_keeps_the_services_writers_off_the_coin(self):
        x = self.x
        rid = self.posted["root_id"]
        coin = x.svc.d.coin
        half = x.svc.guardian_veto(rid, self.guardian, hold=True)
        self.assertTrue(x.svc.lease.busy())
        x.w.advance_to(x.w.h + 11)  # a root is due
        self.assertIsNone(x.svc.operator_step(wait=5, poll=0))
        seq = ClaimSequencer(x.svc)
        seq.queue["p"] = Pending("p", [], Signature.infinity(), [], 0)
        self.assertIsNone(seq.run_batch(wait=5, poll=0))
        with self.assertRaisesRegex(LeaseBusy, "guardian is signing a veto"):
            x.intake.quote(x.request(nonce=3))
        self.assertEqual(x.svc.d.coin, coin, "the coin the lock signed for is still the singleton's")
        x.svc.release_guardian()
        self.assertFalse(x.svc.lease.busy())
        self.assertIsNone(x.svc.lease.guardian())
        del half

    def test_the_hold_lapses_with_the_dispute_window(self):
        x = self.x
        half = x.svc.guardian_veto(self.posted["root_id"], self.guardian, hold=True)
        x.clock.t += half["blocks_left"] * 18.75 + 61
        self.assertIsNone(x.svc.lease.guardian())

    def test_refusals(self):
        x = self.x
        rid = self.posted["root_id"]
        with self.assertRaisesRegex(ValueError, "not the distributor's guardian"):
            x.svc.guardian_veto(rid, b"\x42" * 32)
        with self.assertRaisesRegex(ValueError, "not this distributor's pending root"):
            x.svc.guardian_veto(rid + 1, self.guardian)
        pending = x.svc.d.chain_state.pending
        x.w.advance_to(pending.activates_at + 1)
        with self.assertRaisesRegex(ValueError, "(left its dispute window|not this distributor's pending root)"):
            x.svc.guardian_veto(rid, self.guardian)
        self.assertIsNone(x.svc.lease.guardian(), "a refused veto holds nothing")


if __name__ == "__main__":
    unittest.main()
