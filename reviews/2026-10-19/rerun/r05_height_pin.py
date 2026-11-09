"""FLPCR-05 rerun: the two emitted height conditions (shared.rue prologue)
are pinned (old probes F/mut1.py, F/futureh.py, F/pinprobe.py).

1. On the current pins: a creator-authorized reclaim naming a future `h` is
   refused with AssertHeightAbsoluteFailed; a stale `h` (tip - 32) with
   AssertBeforeHeightAbsoluteFailed. Both are consensus names, so the rule is
   pinned by the error itself.
2. Each guard deleted in turn (the old mut1.py shape, compiled into a scratch
   folder): the same probe is now ACCEPTED (so the probe really exercises the
   guard, unlike the old height-pin test, FLPCR-16), and
   tests/test_chain.py::TestHeightPin fails (the mutant is killed). In the
   review, both mutants survived with 92 tests green.

Covered by TestHeightPin (both assets) and scripts/mutate.py (GUARDS lists
both lines). Needs rue 0.8.4 on PATH for part 2; `--inner` runs part 1 only.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[3]


def inner():
    from common import ASSETS, HEIGHT_WINDOW, LEAD, POOL, ByMessage, attempt, final_root, finish, settle, src, world
    for tag, asset in ASSETS:
        print(f"\n{tag}")
        w, c = world(asset)
        w.create(c, "creator")
        r = final_root(w, c, src((w.keys.ph("bob"), 1)))
        w.post(r)
        settle(w)
        assert w.h < c.end + c.claim_window

        def early_reclaim():
            before = w.balance("creator")
            w.push([w.d.reclaim(c.end + c.claim_window, c.id, ByMessage())])  # the creator authorizes it
            return w.balance("creator") - before

        attempt(f"ATTACK creator reclaim at {w.h} naming h = end + window = {c.end + c.claim_window}", early_reclaim,
                expect="refuse", reason="AssertHeightAbsoluteFailed")

        w2, _ = world(asset)
        w2.advance_to(w2.h + HEIGHT_WINDOW + 2)
        h = w2.h - HEIGHT_WINDOW
        attempt("ATTACK stale h = tip - 32 (create)", w2.create_raw, "creator", 1000, w2.h + LEAD, w2.h + LEAD + 40,
                1, POOL, [10000, 0, 0, None, None, 1], 0, 160, h=h, expect="refuse",
                reason="AssertBeforeHeightAbsoluteFailed")
        attempt("control: the same create at h = tip", w2.create_raw, "creator", 1000, w2.h + LEAD, w2.h + LEAD + 40,
                1, POOL, [10000, 0, 0, None, None, 1], 1, 160)
    finish("FLPCR-05 (current pins)")


def mutant(needle):
    sys.path.insert(0, str(ROOT / "contracts"))
    import build
    tmp = pathlib.Path(tempfile.mkdtemp())
    shutil.copytree(ROOT / "contracts", tmp / "contracts", ignore=shutil.ignore_patterns("compiled"))
    target = tmp / "contracts/puzzles/flpcr/shared.rue"
    lines = target.read_text().splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.strip().startswith(needle)]
    assert len(hits) == 1, hits
    del lines[hits[0]]
    target.write_text("".join(lines))
    build.compile_into(tmp / "contracts", tmp / "compiled")
    return tmp / "compiled"


def outer():
    env = dict(os.environ, PYTHONPATH=f"{ROOT}:{ROOT / 'reviews/2026-10-19/rerun'}")
    env["PATH"] = f"{os.path.expanduser('~/.cargo/bin')}:{env.get('PATH', '')}"
    me = [sys.executable, __file__, "--inner"]
    first = subprocess.run(me, cwd=ROOT, env=env, capture_output=True, text=True)
    print(first.stdout)
    ok = first.returncode == 0
    for needle, probe in (("AssertHeightAbsolute {", "ATTACK creator reclaim"),
                          ("AssertBeforeHeightAbsolute {", "ATTACK stale h")):
        compiled = mutant(needle)
        menv = dict(env, FLPCR_COMPILED=str(compiled))
        run = subprocess.run(me, cwd=ROOT, env=menv, capture_output=True, text=True)
        accepted = [line for line in run.stdout.splitlines() if probe in line and "ACCEPTED" in line]
        tests = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_chain.py", "-k", "TestHeightPin"],
                               cwd=ROOT, env=menv, capture_output=True, text=True)
        killed = tests.returncode != 0
        summary = tests.stdout.strip().splitlines()[-1] if tests.stdout.strip() else tests.stderr[-200:]
        print(f"mutant without `{needle}`:")
        print(f"  [{'PASS' if len(accepted) == 2 else 'FAIL'}] the probe is ACCEPTED on the mutant (XCH and CAT): "
              f"{len(accepted)}/2")
        print(f"  [{'PASS' if killed else 'FAIL'}] TestHeightPin on the mutant fails (killed): {summary}")
        ok = ok and killed and len(accepted) == 2
        shutil.rmtree(compiled.parent)
    print(f"\nFLPCR-05: {'all as expected' if ok else 'UNEXPECTED results above'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    inner() if "--inner" in sys.argv else outer()
