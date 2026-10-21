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
import importlib.util
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
PUZZLES = HERE / "puzzles"
COMPILED = HERE / "compiled"
PINS = HERE / "pins.json"

# The leaf list lives in flpcr/leaves.py, loaded by path so the build needs
# nothing else from the package.
_spec = importlib.util.spec_from_file_location("flpcr_leaves", HERE.parent / "flpcr" / "leaves.py")
_leaves = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_leaves)
UPSTREAM, LEAVES, EXTRA, sources = _leaves.UPSTREAM, _leaves.LEAVES, _leaves.EXTRA, _leaves.sources


class CompileError(Exception):
    pass


def compile_hex(rel: str, project: pathlib.Path = HERE) -> str:
    """Compile one puzzle of the rue project at `project`. Raises CompileError
    on a nonzero exit, no output, or any "Error" rue prints."""
    result = subprocess.run(
        ["rue", "build", "--hex", str(project / "puzzles" / rel)],
        cwd=project,
        capture_output=True,
        text=True,
    )
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    if result.returncode != 0 or not lines or "Error" in result.stdout + result.stderr:
        raise CompileError(f"rue failed on {rel}:\n{result.stdout}\n{result.stderr}")
    return lines[-1].strip()


def compile_into(project: pathlib.Path, out: pathlib.Path) -> None:
    """Compile every puzzle of `project` into `out`/<name>.hex (scripts/mutate.py)."""
    out.mkdir(exist_ok=True)
    for name, rel in sources().items():
        (out / f"{name}.hex").write_text(compile_hex(rel, project) + "\n")


def upstream_source_hashes() -> dict:
    """sha256 of every vendored upstream source, line endings normalized so a
    Windows checkout (CRLF) hashes the same as git's LF."""
    files = sorted((PUZZLES / "upstream").rglob("*.rue"))
    return {
        p.relative_to(PUZZLES).as_posix(): hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for p in files
    }


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
        try:
            hex_program = compile_hex(rel)
        except CompileError as e:
            raise SystemExit(str(e))
        th = tree_hash_of_hex(hex_program)
        built[name] = (hex_program, th)
        group = "upstream" if name in UPSTREAM else "leaves"
        want = pins[group].get(name)
        if group == "upstream" and want != th:
            failures.append(f"upstream {name}: built {th}, pinned {want}")
        if group == "leaves" and not repin and want is not None and want != th:
            failures.append(f"leaf {name}: built {th}, pinned {want} (run --repin if intended)")
        print(f"{name:28s} {th}")

    # The upstream sources are pinned by content too: an edit that happens to
    # compile to the same tree hash still fails here.
    pinned_sources = pins.get("upstream_sources_sha256")
    if pinned_sources is not None and pinned_sources != upstream_source_hashes():
        failures.append("upstream sources differ from upstream_sources_sha256 in pins.json")
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
        pins["leaves"] = {name: built[name][1] for name in LEAVES + EXTRA}
    if pinned_sources is None:
        pins["upstream_sources_sha256"] = upstream_source_hashes()
    PINS.write_text(json.dumps(pins, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
