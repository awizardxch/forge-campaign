"""Delete each `assert` in our leaves, one at a time, and see if a test notices.

    python scripts/mutate.py            # every assert in contracts/puzzles/flpcr/
    python scripts/mutate.py claim      # only claim.rue

For each mutant the whole rue project is copied to a scratch directory, the
one line is removed, the leaves are rebuilt into a scratch compiled/ and the
chain suites run against it (FLPCR_COMPILED). A mutant whose tests still pass
is a SURVIVOR: an assertion no test pins. Each survivor needs a test that
kills it or a written argument in scripts/mutation-arguments.json. A mutant
that does not compile is reported and skipped.

The method is Forge's (forgePuzzleV14.md section 4): "survived" is never
read as "redundant" without asking what else refused first.
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


def build(project: pathlib.Path, out: pathlib.Path) -> bool:
    out.mkdir(exist_ok=True)
    names = {
        "action": "upstream/singleton/action.rue",
        "reserve_finalizer": "upstream/singleton/reserve_finalizer.rue",
        "slot": "upstream/singleton/slot.rue",
        "p2_delegated_by_singleton": "upstream/singleton/p2_delegated_by_singleton.rue",
    }
    for leaf in ["init", "create", "post_root", "veto", "claim", "reclaim", "collect", "rotate"]:
        names[leaf] = f"flpcr/{leaf}.rue"
    for name, rel in names.items():
        r = subprocess.run(["rue", "build", "--hex", str(project / "puzzles" / rel)], cwd=project,
                           capture_output=True, text=True)
        lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
        if r.returncode != 0 or not lines or "Error" in r.stdout + r.stderr:
            return False
        (out / f"{name}.hex").write_text(lines[-1] + "\n")
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
        if not build(tmp / "contracts", tmp / "compiled") or not tests_pass(tmp / "compiled"):
            print("baseline does not pass; fix that first", file=sys.stderr)
            return 2
    survivors, killed, broken = [], 0, 0
    for rue in sorted((CONTRACTS / "puzzles" / "flpcr").glob("*.rue")):
        if only and rue.stem not in only:
            continue
        lines = rue.read_text().splitlines(keepends=True)
        for i, line in enumerate(lines):
            if not line.strip().startswith("assert "):
                continue
            with tempfile.TemporaryDirectory() as tmp:
                tmp = pathlib.Path(tmp)
                shutil.copytree(CONTRACTS, tmp / "contracts", ignore=shutil.ignore_patterns("compiled"))
                target = tmp / "contracts" / "puzzles" / "flpcr" / rue.name
                mutated = lines[:i] + lines[i + 1:]
                target.write_text("".join(mutated))
                label = f"{rue.stem}:{i + 1}: {line.strip()}"
                if not build(tmp / "contracts", tmp / "compiled"):
                    broken += 1
                    print(f"NO BUILD  {label}")
                    continue
                if tests_pass(tmp / "compiled"):
                    key = f"{rue.stem}: {line.strip()}"
                    note = arguments.get(key)
                    print(f"SURVIVED  {label}" + (f"  [argued: {note}]" if note else ""))
                    if not note:
                        survivors.append(label)
                else:
                    killed += 1
                    print(f"killed    {label}")
    print(f"\n{killed} killed, {len(survivors)} unargued survivors, {broken} did not build")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
