"""Run every rerun script and print one line per script.

    python reviews/2026-10-19/rerun/run_all.py          # from the repo root

A script exits 0 when every attempt went as expected (attacks refused for the
pinned reason, controls accepted). r08_final_root.py and n_new_surface.py
exit 1 on the current build: attacks that still get through (README.md)."""

import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCRIPTS = sorted(p for p in HERE.glob("[rn]*.py") if p.name != "run_all.py")
env = dict(os.environ, PYTHONPATH=f"{ROOT}{os.pathsep}{HERE}")
env["PATH"] = f"{os.path.expanduser('~/.cargo/bin')}{os.pathsep}{env.get('PATH', '')}"
bad = []
for script in SCRIPTS:
    r = subprocess.run([sys.executable, str(script)], cwd=ROOT, env=env, capture_output=True, text=True)
    last = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
    print(f"{'ok  ' if r.returncode == 0 else 'FAIL'} {script.name:28s} {last[:150]}", flush=True)
    if r.returncode:
        bad.append(script.name)
sys.exit(1 if bad else 0)
