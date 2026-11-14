"""Mutation check of the B2 fixes (audit 2026-11-11, CE-13 and CP-06): undo each fix's
load-bearing line, and `tests/test_b2_fixes.py` must fail; restore it, and it must pass.

  M1  resync._select runs the selector      -> back to a path walk (`to_int` of the selector)
  M2  resync follows the reserve a spend named -> the follow branch deleted
  M3  pinned_reserve_finalizer: coinid of the CURRIED parent -> the solution's value again
  M4  pinned_reserve_finalizer: the next finalizer re-curried with this reserve -> with the old parent
  and each CP-08 / CE-02 assert of the 2026-11-13 leaf re-pin, deleted on its own
  M5  v16_fees runs a pool spend's selector (CE-13's sibling, V16 and V17 pools) -> a path walk
      (tests/test_v17_pools.py)

M1 and M2 edit a temporary copy of the package; M3 and M4 rebuild the finalizer with rue into a
temporary compiled folder and point the driver at it (FLPCR_COMPILED). Prints one verdict per
mutant and exits nonzero if any mutant survives or the unmutated suite fails.

    .venv/Scripts/python.exe scripts/mutate_b2_fixes.py
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST = "tests/test_b2_fixes.py"
V17_TEST = "tests/test_v17_pools.py"
sys.path.insert(0, str(ROOT / "contracts"))
from build import compile_hex  # noqa: E402

PY_MUTANTS = {
    "M1 selector run -> path walk": (
        TEST, "flpcr/chain/resync.py",
        "        return selector.run(leaves, MAX_COST, False).value\n",
        "        node, path = leaves, to_int(selector)\n"
        "        while path > 1:\n"
        "            node = node.rest() if path & 1 else node.first()\n"
        "            path >>= 1\n"
        "        return node\n",
    ),
    "M2 reserve follow deleted": (
        TEST, "flpcr/chain/resync.py",
        "            _reserve_at(reader, clvm, d, named, d.chain_state.reserve)\n"
        "            out.reserve_forks.append((out.generations, height, d.reserve_coin.coin_id().hex()))\n",
        "            pass\n",
    ),
    "M5 fee reader selector run -> path walk": (
        V17_TEST, "flpcr/v16_fees.py",
        "            return selector.run(puzzles, 11_000_000_000, False).value\n",
        "            node, path = puzzles, selector.to_int()\n"
        "            while path > 1:\n"
        "                node = node.rest() if path & 1 else node.first()\n"
        "                path >>= 1\n"
        "            return node\n",
    ),
}

RUE_MUTANTS = {
    "M3 curried parent -> solution parent": ("pinned_reserve_finalizer", [
        ("    ..._ignored: Any,\n", "    ...solution_parent: Bytes32,\n"),
        ("    let reserve_id = coinid(\n        RESERVE_PARENT_ID,\n", "    let reserve_id = coinid(\n        solution_parent,\n"),
    ]),
    "M4 next finalizer keeps the old parent": ("pinned_reserve_finalizer", [
        ("                        tree_hash_atom(reserve_id),\n", "                        tree_hash_atom(RESERVE_PARENT_ID),\n"),
    ]),
}
# CP-08 and CE-02 (re-pin 2026-11-13): each new assert deleted on its own.
for _leaf, _line in [
    ("create", "    assert amount == amount + 0;\n"),
    ("create", "    assert start == start + 0;\n"),
    ("create", "    assert end == end + 0;\n"),
    ("create", "    assert claim_window == claim_window + 0;\n"),
    ("create", "    assert target.length == 32;\n"),
    ("post_root", "    assert start == start + 0;\n"),
    ("post_root", "    assert end == end + 0;\n"),
    ("post_root", "    assert root.length == 32;\n"),
    ("post_root", "        assert total == total + 0;  // CP-08: a stored total has one spelling\n"),
    ("claim", "    assert (insert == 0) | (insert == 1);\n"),
    ("delegate", "    assert (insert == 0) | (insert == 1);\n"),
]:
    RUE_MUTANTS[f"{_leaf}: delete `{_line.strip()}`"] = (_leaf, [(_line, "")])


def run_tests(cwd: pathlib.Path, env: dict, test: str = TEST) -> tuple[bool, str]:
    out = subprocess.run([sys.executable, "-m", "pytest", test, "-q", "-p", "no:cacheprovider"], cwd=cwd, env=env,
                         capture_output=True, text=True)
    last = [l for l in out.stdout.strip().splitlines() if l.strip()][-1:] or ["(no output)"]
    return out.returncode == 0, last[0]


def main() -> int:
    env = dict(os.environ)
    ok, line = run_tests(ROOT, env)
    print(f"unmutated: {'pass' if ok else 'FAIL'} ({line})")
    bad = not ok
    ok17, line17 = run_tests(ROOT, env, V17_TEST)
    print(f"unmutated {V17_TEST}: {'pass' if ok17 else 'FAIL'} ({line17})")
    bad |= not ok17
    for name, (test, rel, old, new) in PY_MUTANTS.items():
        with tempfile.TemporaryDirectory() as tmp:
            copy = pathlib.Path(tmp) / "fc"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", ".pytest_cache",
                                                                       "testnet-state", "node_modules"))
            path = copy / rel
            src = path.read_text()
            if src.count(old) != 1:
                print(f"{name}: the line to mutate is not there once; fix this script")
                return 2
            path.write_text(src.replace(old, new))
            passed, line = run_tests(copy, env, test)
        verdict = "SURVIVED" if passed else "killed"
        bad |= passed
        print(f"{name}: {verdict} ({line})")
    for name, (puzzle, edits) in RUE_MUTANTS.items():
        rel = f"flpcr/{puzzle}.rue"
        with tempfile.TemporaryDirectory() as tmp:
            project = pathlib.Path(tmp) / "contracts"
            shutil.copytree(ROOT / "contracts", project, ignore=shutil.ignore_patterns("compiled"))
            src = (ROOT / "contracts" / "puzzles" / rel).read_text()
            for o, n in edits:
                if src.count(o) != 1:
                    print(f"{name}: the line to mutate is not there once; fix this script")
                    return 2
                src = src.replace(o, n)
            (project / "puzzles" / rel).write_text(src)
            compiled = pathlib.Path(tmp) / "compiled"
            shutil.copytree(ROOT / "contracts" / "compiled", compiled)
            try:
                hex_program = compile_hex(rel, project)
            except Exception as e:  # noqa: BLE001
                print(f"{name}: did not build ({str(e)[:120]})")
                continue
            (compiled / f"{puzzle}.hex").write_text(hex_program + "\n")
            passed, line = run_tests(ROOT, {**env, "FLPCR_COMPILED": str(compiled)})
        verdict = "SURVIVED" if passed else "killed"
        bad |= passed
        print(f"{name}: {verdict} ({line})")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
