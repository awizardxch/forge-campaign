import random
import unittest

from flpcr.clvm import int_to_atom, sha256tree
from flpcr.merkle import build_tree, claim_leaf, root_from_proof

# `clvm` is optional: when installed, the encoding and hashing are checked against it.
try:
    from clvm import SExp as _SExp
    from clvm.casts import int_to_bytes as _int_to_bytes
except ImportError:
    _SExp = None


def _reference_tree_hash(sexp) -> bytes:
    from hashlib import sha256

    if sexp.listp():
        left, right = sexp.pair
        return sha256(b"\x02" + _reference_tree_hash(_SExp.to(left)) + _reference_tree_hash(_SExp.to(right))).digest()
    return sha256(b"\x01" + sexp.atom).digest()


class TestClvm(unittest.TestCase):
    @unittest.skipIf(_SExp is None, "clvm not installed")
    def test_int_encoding_matches_clvm(self):
        rng = random.Random(7)
        samples = [0, 1, -1, 127, 128, -128, -129, 255, 256, 2**64, -(2**64), 10**30]
        samples += [rng.randint(-(2**80), 2**80) for _ in range(500)]
        for n in samples:
            self.assertEqual(int_to_atom(n), _int_to_bytes(n), n)

    @unittest.skipIf(_SExp is None, "clvm not installed")
    def test_tree_hash_matches_clvm(self):
        value = ["flpcr-claim-v1", b"\xaa" * 32, b"\xcc" * 32, b"\xbb" * 32, 1_000_000_000_000]
        self.assertEqual(sha256tree(value), _reference_tree_hash(_SExp.to(value)))
        pair = (b"\x01" * 32, 42)
        self.assertEqual(sha256tree(pair), _reference_tree_hash(_SExp.to(pair)))

    def test_nil_hash(self):
        # sha256(0x01) is the well-known hash of nil.
        self.assertEqual(
            sha256tree([]).hex(),
            "4bf5122f344554c53bde2ebb8cd2b7e3d1600ad631c385a5d7cce23c7785459a",
        )


class TestMerkle(unittest.TestCase):
    def test_every_proof_verifies_for_many_sizes(self):
        for n in [1, 2, 3, 4, 5, 7, 8, 9, 31, 64, 100]:
            leaves = [claim_leaf(b"\x11" * 32, b"\x33" * 32, bytes([i % 256]) * 32, i + 1) for i in range(n)]
            root, proofs = build_tree(leaves)
            for leaf in leaves:
                self.assertEqual(root_from_proof(leaf, proofs[leaf]), root)

    def test_proof_does_not_verify_another_leaf(self):
        leaves = [claim_leaf(b"\x11" * 32, b"\x33" * 32, bytes([i]) * 32, 5) for i in range(4)]
        root, proofs = build_tree(leaves)
        forged = claim_leaf(b"\x11" * 32, b"\x33" * 32, bytes([0]) * 32, 6)
        self.assertNotEqual(root_from_proof(forged, proofs[leaves[0]]), root)

    def test_leaf_is_bound_to_distributor_and_campaign(self):
        a = claim_leaf(b"\x11" * 32, b"\x33" * 32, b"\x22" * 32, 5)
        self.assertNotEqual(a, claim_leaf(b"\x12" * 32, b"\x33" * 32, b"\x22" * 32, 5))
        self.assertNotEqual(a, claim_leaf(b"\x11" * 32, b"\x34" * 32, b"\x22" * 32, 5))

    def test_empty_and_duplicates_refused(self):
        with self.assertRaises(ValueError):
            build_tree([])
        with self.assertRaises(ValueError):
            build_tree([b"\x00" * 32, b"\x00" * 32])


if __name__ == "__main__":
    unittest.main()
