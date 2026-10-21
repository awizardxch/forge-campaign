"""The one list of distributor puzzles.

The order of LEAVES fixes the merkle tree, so it fixes every distributor's
puzzle hash: `contracts/build.py`, `scripts/mutate.py` and the driver all read
it from here. Plain data, no imports, so build tooling can load it without the
rest of the package.
"""

# Our leaves, in merkle order: contracts/puzzles/flpcr/<name>.rue.
LEAVES = ["init", "create", "post_root", "veto", "claim", "reclaim", "rotate", "delegate", "dispute", "resolve"]

# Our puzzles that are not leaves: compiled and pinned beside them.
EXTRA = ["bond_launcher"]

# Yakuhito's slot-machine at 2d37ba1, vendored verbatim under contracts/puzzles/.
UPSTREAM = {
    "action": "upstream/singleton/action.rue",
    "finalizer": "upstream/singleton/finalizer.rue",
    "reserve_finalizer": "upstream/singleton/reserve_finalizer.rue",
    "slot": "upstream/singleton/slot.rue",
    "p2_delegated_by_singleton": "upstream/singleton/p2_delegated_by_singleton.rue",
}


def sources() -> dict:
    """name -> path under contracts/puzzles/, for every puzzle we compile."""
    out = dict(UPSTREAM)
    out.update({name: f"flpcr/{name}.rue" for name in LEAVES + EXTRA})
    return out
