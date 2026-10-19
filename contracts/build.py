"""Compile every puzzle with rue and check the pins.

    python contracts/build.py           # rebuild compiled/*.hex, refuse on any pin mismatch
    python contracts/build.py --check   # compile to memory only; exit 1 if anything differs
    python contracts/build.py --repin   # rebuild and rewrite pins.json (our leaves only)

Upstream puzzles are Yakuhito's slot-machine at 2d37ba1, vendored verbatim
under puzzles/upstream/. Their tree hashes are pinned to the values upstream
publishes; `--repin` never touches them, so an edited upstream file always
fails the build. Our leaves are pinned too, so a change to one is a reviewed
diff in pins.json rather than a silent new hash.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
PUZZLES = HERE / "puzzles"
COMPILED = HERE / "compiled"
PINS = HERE / "pins.json"

UPSTREAM = {
    "action": "upstream/singleton/action.rue",
    "finalizer": "upstream/singleton/finalizer.rue",
    "reserve_finalizer": "upstream/singleton/reserve_finalizer.rue",
    "slot": "upstream/singleton/slot.rue",
    "p2_delegated_by_singleton": "upstream/singleton/p2_delegated_by_singleton.rue",
}
LEAVES = ["init", "create", "post_root", "veto", "claim", "reclaim", "collect", "rotate", "delegate"]


def sources() -> dict:
    out = dict(UPSTREAM)
    out.update({name: f"flpcr/{name}.rue" for name in LEAVES})
    return out


def compile_hex(rel: str) -> str:
    result = subprocess.run(
        ["rue", "build", "--hex", str(PUZZLES / rel)],
        cwd=HERE,
        capture_output=True,
        text=True,
    )
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    if result.returncode != 0 or not lines:
        raise SystemExit(f"rue failed on {rel}:\n{result.stdout}\n{result.stderr}")
    return lines[-1].strip()


def tree_hash_of_hex(hex_program: str) -> str:
    from chia_wallet_sdk import Clvm

    return Clvm().deserialize(bytes.fromhex(hex_program)).tree_hash().hex()


def main(argv: list[str]) -> int:
    check = "--check" in argv
    repin = "--repin" in argv
    pins = json.loads(PINS.read_text()) if PINS.exists() else {"upstream": {}, "leaves": {}}
    built = {}
    failures = []
    for name, rel in sources().items():
        hex_program = compile_hex(rel)
        th = tree_hash_of_hex(hex_program)
        built[name] = (hex_program, th)
        group = "upstream" if name in UPSTREAM else "leaves"
        want = pins[group].get(name)
        if group == "upstream" and want != th:
            failures.append(f"upstream {name}: built {th}, pinned {want}")
        if group == "leaves" and not repin and want is not None and want != th:
            failures.append(f"leaf {name}: built {th}, pinned {want} (run --repin if intended)")
        print(f"{name:28s} {th}")

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    if check:
        for name, (hex_program, _) in built.items():
            path = COMPILED / f"{name}.hex"
            if not path.exists() or path.read_text().strip() != hex_program:
                print(f"compiled/{name}.hex is stale", file=sys.stderr)
                return 1
        return 0

    COMPILED.mkdir(exist_ok=True)
    for name, (hex_program, _) in built.items():
        (COMPILED / f"{name}.hex").write_text(hex_program + "\n")
    if repin or not pins["leaves"]:
        pins["leaves"] = {name: built[name][1] for name in LEAVES}
    pins["upstream_sources_sha256"] = {
        rel: hashlib.sha256((PUZZLES / rel).read_bytes()).hexdigest()
        for rel in sorted(
            str(p.relative_to(PUZZLES)) for p in (PUZZLES / "upstream").rglob("*.rue")
        )
    }
    PINS.write_text(json.dumps(pins, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
