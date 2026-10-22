"""The claim sequencer on the simulator: wallets claim by key signature and pay the fee from
a coin of their own, a lock (any puzzle) claims by a mode-16 message in its own spend, and
every bad request is refused at intake, never inside a batch."""

import types
import unittest

try:
    from chia_wallet_sdk import Clvm, SecretKey, Signature

    from flpcr.chain.driver import HEIGHT_WINDOW
    from flpcr.chain.signer import KeySigner
    from flpcr.chain.sim import SimWorld
    from flpcr.chain.values import to_program
    from flpcr.service.chain import SimChain, bundle_json
    from flpcr.service.core import CampaignService, OperatorKey
    from flpcr.service.sequencer import FEE_PER_CLAIM, ClaimSequencer

    HAVE_CHAIN = True
except (ImportError, FileNotFoundError):  # pragma: no cover
    HAVE_CHAIN = False

from flpcr import FixtureSource, HolderEvent, Kind

POOL, LP = b"\xb0" * 32, b"\x1b" * 32


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestSequencer(unittest.TestCase):
    def setUp(self):
        sk = SecretKey.from_seed(bytes([7]) * 32).derive_synthetic()
        op = OperatorKey(sk)
        w = self.w = SimWorld(engine_ph=op.puzzle_hash)
        w.wallets[op.puzzle_hash] = types.SimpleNamespace(sk=sk, pk=sk.public_key(), puzzle_hash=op.puzzle_hash)
        w.sim.new_coin(op.puzzle_hash, 10**13)
        w.advance_to(w.h + 3)
        self.holders = [w.sim.bls(0) for _ in range(3)]  # no coins: they claim by key
        self.lock_ph = w.keys.ph("lock")  # any puzzle: stands in for a multisig lock
        events = [HolderEvent(0, x.puzzle_hash, 1 + i) for i, x in enumerate(self.holders)] + [HolderEvent(0, self.lock_ph, 2)]
        self.src = FixtureSource(lp_assets={POOL: LP}, events={LP: events})
        start = w.h + HEIGHT_WINDOW + 2
        self.c = w.campaign("creator", 1_000_000, start, start + 40, Kind.LP_HOLD, POOL, claim_window=56)
        w.create(self.c, "creator")
        self.svc = CampaignService(SimChain(w.sim), w.launcher_id, "testnet11", lambda cs: self.src, operator=op, every=10)
        self.svc.boot()
        w.advance_to(self.c.end + 1)
        self.svc.operator_step(wait=5, poll=0)
        w.advance_to(self.svc.status()["pending"]["activates_at"] + 1)
        self.seq = ClaimSequencer(self.svc)
        self.funder = w.sim.bls(10**12)

    def leaf(self, ph):
        return next(x for x in self.svc.claimable(ph)["claims"] if x["campaign"] == self.c.id.hex())

    def fee_bundle(self, fee, extra_spends=(), extra_sig=None):
        clvm = Clvm()
        coin = self.w.sim.new_coin(self.funder.puzzle_hash, 10**11)
        clvm.spend_standard_coin(coin, self.funder.pk, clvm.delegated_spend(
            [to_program(clvm, [51, self.funder.puzzle_hash, coin.amount - fee])]))
        spends = list(clvm.coin_spends()) + list(extra_spends)
        sig = KeySigner([self.funder.sk], bytes.fromhex("37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615")).sign(spends)
        if extra_sig is not None:
            sig = Signature.aggregate([sig, extra_sig])
        return bundle_json(spends, sig)

    def key_request(self, wallets, fee=None):
        claims, sigs = [], []
        for x in wallets:
            claims.append({"campaign": self.c.id.hex(), "puzzle_hash": x.puzzle_hash.hex(), "public_key": x.pk.to_bytes().hex()})
            sigs.append(x.sk.sign(bytes.fromhex(self.leaf(x.puzzle_hash)["leaf"])))
        return {"claims": claims, "signature": Signature.aggregate(sigs).to_bytes().hex(),
                "spend_bundle": self.fee_bundle(FEE_PER_CLAIM * len(wallets) if fee is None else fee)}

    def lock_request(self, message=None, fee=FEE_PER_CLAIM):
        leaf = bytes.fromhex(self.leaf(self.lock_ph)["leaf"])
        coin = self.w.sim.new_coin(self.lock_ph, 10**9)
        conds = [[66, 16, b"k" + leaf if message is None else message], [51, self.lock_ph, coin.amount - fee]]
        spend = self.w.keys.spend(coin, "lock", conds)
        return {"claims": [{"campaign": self.c.id.hex(), "puzzle_hash": self.lock_ph.hex()}],
                "spend_bundle": bundle_json([spend], Signature.infinity())}

    def balance(self, ph):
        return sum(c.amount for c in self.w.sim.unspent_coins(ph, False))

    def test_wallets_and_a_lock_in_one_bundle(self):
        owed = {x.puzzle_hash: self.leaf(x.puzzle_hash)["claimable"] for x in self.holders}
        owed[self.lock_ph] = self.leaf(self.lock_ph)["claimable"]
        before = {ph: self.balance(ph) for ph in owed}
        a = self.seq.submit(self.key_request(self.holders[:2]))
        b = self.seq.submit(self.key_request(self.holders[2:]))
        c = self.seq.submit(self.lock_request())
        out = self.seq.run_batch(wait=5, poll=0)
        self.assertTrue(out["pushed"], out)
        self.assertEqual(out["claims"], 4)
        for req in (a, b, c):
            self.assertEqual(self.seq.status_of(req["request"])["status"], "confirmed")
        # The lock's request spent a coin minted after `before` was read: its change comes back too.
        change = {self.lock_ph: 10**9 - FEE_PER_CLAIM}
        for ph, amount in owed.items():
            self.assertEqual(self.balance(ph) - before[ph], amount + change.get(ph, 0), ph.hex()[:8])
        self.assertEqual(self.leaf(self.holders[0].puzzle_hash)["claimable"], 0)

    def test_refused_at_intake(self):
        x, y = self.holders[0], self.holders[1]
        forged = self.key_request([x])
        forged["signature"] = y.sk.sign(b"not the leaf").to_bytes().hex()
        with self.assertRaisesRegex(ValueError, "does not verify"):
            self.seq.submit(forged)
        wrong_key = self.key_request([x])
        wrong_key["claims"][0]["public_key"] = y.pk.to_bytes().hex()
        with self.assertRaisesRegex(ValueError, "does not own"):
            self.seq.submit(wrong_key)
        with self.assertRaisesRegex(ValueError, "need"):
            self.seq.submit(self.key_request([x], fee=1))
        with self.assertRaisesRegex(ValueError, "no spend sends the claim message"):
            self.seq.submit(self.lock_request(message=b"k" + b"\x00" * 32))
        stranger = self.w.sim.bls(0)
        with self.assertRaisesRegex(ValueError, "nothing to claim"):
            self.seq.submit({"claims": [{"campaign": self.c.id.hex(), "puzzle_hash": stranger.puzzle_hash.hex(),
                                         "public_key": stranger.pk.to_bytes().hex()}], "signature": "", "spend_bundle": None})
        self.assertEqual(self.seq.queue, {})

    def test_a_coin_that_is_not_on_chain(self):
        req = self.key_request([self.holders[0]])
        req["spend_bundle"]["coin_spends"][0]["coin"]["amount"] = 10**11 + 1  # names a coin that does not exist
        with self.assertRaisesRegex(ValueError, "not an unspent coin|does not verify"):
            self.seq.submit(req)

    def test_claimed_twice_is_refused_after_the_first_lands(self):
        self.seq.submit(self.key_request([self.holders[0]]))
        self.seq.run_batch(wait=5, poll=0)
        with self.assertRaisesRegex(ValueError, "nothing to claim"):
            self.seq.submit(self.key_request([self.holders[0]]))


if __name__ == "__main__":
    unittest.main()
