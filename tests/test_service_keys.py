"""The host's self-made keys and operator rotation (owner, 2026-11-28: "add a new self generated key
with rotation and not reuse the same env")."""

import json
import pathlib
import tempfile
import unittest

from test_service_follow import HAVE_CHAIN, ServiceTest

try:
    from chia_wallet_sdk import SecretKey, standard_puzzle_hash

    from flpcr.service import keys
    from flpcr.service.core import OperatorKey
except ImportError:  # pragma: no cover
    keys = None


@unittest.skipUnless(keys is not None, "chia-wallet-sdk missing")
class TestKeyFile(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.path = self.dir / "keys" / "mainnet.json"

    def test_made_once_then_read_back(self):
        logged = []
        ring = keys.load_or_create(self.path, "mainnet", log=logged.append)
        self.assertTrue(self.path.exists())
        again = keys.load_or_create(self.path, "mainnet", log=logged.append)
        self.assertEqual(again.router.to_bytes(), ring.router.to_bytes(), "the second start reads, never remakes")
        self.assertEqual([k.to_bytes() for k in again.operators], [k.to_bytes() for k in ring.operators])
        self.assertEqual(len(logged), 1, "only the first start makes keys")
        self.assertNotIn(ring.router.to_bytes().hex(), logged[0], "the log never holds a secret")
        self.assertNotEqual(ring.router_ph, ring.operator.puzzle_hash, "two separate keys")

    def test_a_key_file_is_never_overwritten(self):
        keys.load_or_create(self.path, "mainnet", log=lambda m: None)
        with self.assertRaises(FileExistsError):
            keys._write(self.path, keys.KeyRing("mainnet", keys.fresh_key(), [keys.fresh_key()]), replace=False)

    def test_create_refuses_a_checkout_and_an_existing_file(self):
        repo = pathlib.Path(__file__).resolve().parent / "_keys_here.json"
        try:
            with self.assertRaises(ValueError):
                keys.create(repo, "mainnet")
            self.assertFalse(repo.exists(), "nothing written inside the checkout")
        finally:
            repo.unlink(missing_ok=True)  # a broken guard must not leave a key in the checkout
        ring = keys.create(self.path, "mainnet")
        self.assertEqual(keys.load(self.path, "mainnet").router.to_bytes(), ring.router.to_bytes())
        with self.assertRaises(FileExistsError):
            keys.create(self.path, "mainnet")

    def test_another_networks_file_is_refused(self):
        keys.load_or_create(self.path, "mainnet", log=lambda m: None)
        with self.assertRaises(ValueError):
            keys.load(self.path, "testnet11")

    def test_public_names_no_secret(self):
        ring = keys.load_or_create(self.path, "mainnet", log=lambda m: None)
        text = json.dumps(ring.public())
        for sk in [ring.router, *ring.operators]:
            self.assertNotIn(sk.to_bytes().hex(), text)
        self.assertTrue(ring.public()["router"]["address"].startswith("xch1"))

    def test_new_operator_keeps_the_old_ones(self):
        ring = keys.load_or_create(self.path, "mainnet", log=lambda m: None)
        first = ring.operator.puzzle_hash
        ring2 = keys.new_operator(self.path, "mainnet")
        self.assertEqual([k.puzzle_hash for k in ring2.all_operators][0], first, "the old operator stays held")
        self.assertNotEqual(ring2.operator.puzzle_hash, first, "the new one launches")
        self.assertEqual(ring2.router.to_bytes(), ring.router.to_bytes(), "the router is not rotated")
        self.assertEqual(len(keys.load(self.path, "mainnet").operators), 2, "written to the file")

    def test_env_keys_or_the_file_not_both(self):
        op, router = keys.fresh_key(), keys.fresh_key()
        env = {"FLPCR_OPERATOR_SK": op.to_bytes().hex(), "FLPCR_ROUTER_SK": router.to_bytes().hex()}
        ring = keys.from_env(env, "testnet11", log=lambda m: None)
        self.assertEqual(ring.operator.puzzle_hash, standard_puzzle_hash(op.public_key()))
        self.assertIsNone(keys.from_env({}, "mainnet"), "neither: reads and claims only")
        with self.assertRaises(ValueError):
            keys.from_env({**env, "FLPCR_KEY_FILE": str(self.path)}, "mainnet")
        made = keys.from_env({"FLPCR_KEY_FILE": str(self.path)}, "mainnet", log=lambda m: None)
        self.assertTrue(self.path.exists())
        self.assertEqual(keys.from_env({"FLPCR_KEY_FILE": str(self.path)}, "mainnet").router.to_bytes(),
                         made.router.to_bytes())


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestOperatorRotation(ServiceTest):
    """The guardian rotates a distributor to another key the host holds: the service signs the
    next root with it, paying from the old address's XCH until the new one has its own."""

    def setUp(self):
        super().setUp()
        sk = SecretKey.from_seed(bytes([8]) * 32).derive_synthetic()
        self.op2 = OperatorKey(sk)

    def rotate_to_op2(self, svc):
        w = self.w
        self.world_follows()
        w.push([w.d.rotate(w.h, self.op2.puzzle_hash, sender=w.keys.ph("guardian"))])
        w.advance_to(w.d.chain_state.rotation.effective_at)
        svc.follow()
        self.assertEqual(svc.d.engine_at(w.h), self.op2.puzzle_hash, "the rotation landed")

    def test_the_next_root_is_signed_with_the_new_key(self):
        w = self.w
        svc = self.service(every=10, operators=[self.op2])
        w.advance_to(self.c.start + 12)
        self.assertIsNotNone(svc.operator_step(wait=5, poll=0), "root 1 under the first key")
        self.rotate_to_op2(svc)
        old = self.op_balance()
        self.assertEqual(w.xch_balance(self.op2.puzzle_hash), 0, "the new key starts empty")
        posted = svc.operator_step(wait=5, poll=0)
        self.assertIsNotNone(posted, "root 2 under the new key")
        self.assertEqual(self.op_balance(), 0, "the old address's coin moved to the new one")
        self.assertEqual(w.xch_balance(self.op2.puzzle_hash), old - posted["fee"])

    def test_a_host_without_the_new_key_stops(self):
        w = self.w
        svc = self.service(every=10)
        w.advance_to(self.c.start + 12)
        svc.operator_step(wait=5, poll=0)
        self.rotate_to_op2(svc)
        with self.assertRaises(RuntimeError) as e:
            svc.operator_step(wait=5, poll=0)
        self.assertIn("not this distributor's engine", str(e.exception), "refused before building a spend")


if __name__ == "__main__":
    unittest.main()
