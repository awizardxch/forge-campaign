"""Delete each assertion and each emitted guard in our leaves, one at a time,
and see if a test notices.

    python scripts/mutate.py            # every mutable line in contracts/puzzles/flpcr/
    python scripts/mutate.py claim      # only claim.rue

Two kinds of line are mutated:
  * `assert ...;` lines;
  * lines that emit a condition the leaf's safety rests on, one list element
    per line (see GUARDS): the height pin, authorizations, the delegation fee
    and slot spends. An `assert` is not the only way a leaf refuses; a deleted
    AssertHeightAbsolute let `h` be overstated with every suite still green
    (review 2026-10-19, FLPCR-05).

For each mutant the whole rue project is copied to a scratch directory, the
one line is removed, every puzzle is rebuilt with contracts/build.py into a
scratch compiled/ and the chain suites run against it (FLPCR_COMPILED). A
mutant whose tests still pass is a SURVIVOR: a line no test pins. Each
survivor needs a test that kills it or a written argument in
scripts/mutation-arguments.json. A mutant that does not compile is reported
and skipped.

The method is Forge's mutation rule, unchanged through V16 (first written up in
forgePuzzleV14.md section 4): "survived" is never read as "redundant" without
asking what else refused first.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
ARGUMENTS = ROOT / "scripts" / "mutation-arguments.json"
SUITES = ["tests/test_chain.py", "tests/test_differential.py"]

sys.path.insert(0, str(CONTRACTS))
import build  # noqa: E402  (contracts/build.py: one compile path for the build and the mutants)

# A line starting with one of these, ending in ",", is one element of a
# condition list and can be deleted on its own.
GUARDS = (
    "AssertHeightAbsolute",
    "AssertBeforeHeightAbsolute",
    "AggSigMe",
    "ReserveFee",
    "receive_from(",
    "authorize(",
    "authorize_by_spend(",
    "authorize_claim(",
    "spend_claim_slot(",
    "spend_delegation_slot(",
)


def mutable(line: str) -> bool:
    s = line.strip()
    if s.startswith("assert "):
        return True
    return s.startswith(GUARDS) and s.endswith(",")


def compiles(project: pathlib.Path, out: pathlib.Path) -> bool:
    try:
        build.compile_into(project, out)
    except build.CompileError:
        return False
    return True


def tests_pass(compiled: pathlib.Path) -> bool:
    env = dict(os.environ, FLPCR_COMPILED=str(compiled))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", *SUITES], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    return r.returncode == 0


def main(argv) -> int:
    only = set(argv)
    arguments = json.loads(ARGUMENTS.read_text()) if ARGUMENTS.exists() else {}
    # A failing baseline would make every mutant look killed.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        shutil.copytree(CONTRACTS, tmp / "contracts", ignore=shutil.ignore_patterns("compiled"))
        if not compiles(tmp / "contracts", tmp / "compiled") or not tests_pass(tmp / "compiled"):
            print("baseline does not pass; fix that first", file=sys.stderr)
            return 2
    survivors, killed, broken = [], 0, 0
    for rue in sorted((CONTRACTS / "puzzles" / "flpcr").glob("*.rue")):
        if only and rue.stem not in only:
            continue
        lines = rue.read_text().splitlines(keepends=True)
        for i, line in enumerate(lines):
            if not mutable(line):
                continue
            with tempfile.TemporaryDirectory() as tmp:
                tmp = pathlib.Path(tmp)
                shutil.copytree(CONTRACTS, tmp / "contracts", ignore=shutil.ignore_patterns("compiled"))
                target = tmp / "contracts" / "puzzles" / "flpcr" / rue.name
                target.write_text("".join(lines[:i] + lines[i + 1:]))
                label = f"{rue.stem}:{i + 1}: {line.strip()}"
                if not compiles(tmp / "contracts", tmp / "compiled"):
                    broken += 1
                    print(f"NO BUILD  {label}", flush=True)
                    continue
                if tests_pass(tmp / "compiled"):
                    key = f"{rue.stem}: {line.strip()}"
                    note = arguments.get(key)
                    print(f"SURVIVED  {label}" + (f"  [argued: {note}]" if note else ""), flush=True)
                    if not note:
                        survivors.append(label)
                else:
                    killed += 1
                    print(f"killed    {label}", flush=True)
    print(f"\n{killed} killed, {len(survivors)} unargued survivors, {broken} did not build")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
