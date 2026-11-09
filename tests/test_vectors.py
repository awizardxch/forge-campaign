"""fixtures/vectors.json, the reference forge-ui's TypeScript mirrors check against, must match the code."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestVectors(unittest.TestCase):
    def test_vectors_are_current(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_vectors.py"), "--check"],
                           cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_stand_in_signs_only_its_pairs(self):
        from chia_wallet_sdk import Clvm

        v = json.loads((ROOT / "fixtures" / "vectors.json").read_text())
        s = v["claim_stand_in"]
        clvm = Clvm()
        puzzle = clvm.deserialize(bytes.fromhex(s["puzzle_reveal"]))
        self.assertEqual(puzzle.tree_hash().hex(), s["puzzle_hash"])
        out = puzzle.run(clvm.nil(), 11_000_000_000, False).value
        conditions = [list(c.to_list()) for c in out.to_list()]
        self.assertEqual(len(conditions), len(s["pairs"]))
        for cond, pair in zip(conditions, s["pairs"]):
            self.assertEqual(cond[0].to_int(), 49)  # AGG_SIG_UNSAFE: never a spendable condition
            self.assertEqual(cond[1].to_atom().hex(), pair["public_key"])
            self.assertEqual(cond[2].to_atom().hex(), pair["leaf"])


if __name__ == "__main__":
    unittest.main()
