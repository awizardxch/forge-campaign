"""Delete one line of a leaf, rebuild into scratch, run chosen tests. usage: mut1.py file.rue "line text substring" test_ids..."""
import sys, shutil, subprocess, pathlib, os, tempfile
sys.path.insert(0, __import__('pathlib').Path(__file__).resolve().parents[4].as_posix() + "/scripts")
import mutate
ROOT = pathlib.Path(__file__).resolve().parents[4]
rue, needle, tests = sys.argv[1], sys.argv[2], sys.argv[3:]
tmp = pathlib.Path(tempfile.mkdtemp())
shutil.copytree(ROOT/"contracts", tmp/"contracts", ignore=shutil.ignore_patterns("compiled"))
t = tmp/"contracts/puzzles/flpcr"/rue
lines = t.read_text().splitlines(keepends=True)
hits = [i for i,l in enumerate(lines) if needle in l]
assert len(hits)==1, hits
print("removing:", lines[hits[0]].strip())
del lines[hits[0]]
t.write_text("".join(lines))
assert mutate.build(tmp/"contracts", tmp/"compiled"), "no build"
env = dict(os.environ, FLPCR_COMPILED=str(tmp/"compiled"))
r = subprocess.run([sys.executable, "-m", "pytest", "-q", *tests], cwd=ROOT, env=env, capture_output=True, text=True)
print(r.stdout[-3000:]); print(r.stderr[-2000:])
