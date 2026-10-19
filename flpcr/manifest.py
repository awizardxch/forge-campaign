"""The published form of a root and of the engine's state.

A root manifest is what the operator pins (IPFS or object storage) and names by
hash in the `post_root` spend, like Merkl's `ipfsHash`. It holds every claim
with its proof, so a claim can be built from the manifest alone. Fractions are
written as [numerator, denominator] so a replay in another language is exact.

The manifest also commits the address link log the root used: `links.count`
entries (every entry with a height below `end`) whose canonical JSON hashes to
`links.sha256`. The log itself (`flpcr.links.log_to_json`) is published next
to the manifest, so a watcher can replay a root whose gates or boosts depend
on links, and can tell if a later log rewrites it.
"""

from __future__ import annotations

import json
from fractions import Fraction
from hashlib import sha256

from .engine import EngineState, Root
from .links import EMPTY_LOG_HASH


def root_to_json(root: Root) -> dict:
    return {
        "distributor_id": root.distributor_id.hex(),
        "root_id": root.root_id,
        "start": root.start,
        "end": root.end,
        "root": root.root.hex() if root.root else None,
        "claim_count": len(root.claims),
        "totals": {cid.hex(): amt for cid, amt in sorted(root.totals.items())},
        "links": {"count": root.link_count, "sha256": root.links_hash.hex()},
        "claims": [
            {
                "campaign_id": c.campaign_id.hex(),
                "puzzle_hash": c.puzzle_hash.hex(),
                "cumulative": c.cumulative,
                "proof": {"path": c.proof[0], "hashes": [h.hex() for h in c.proof[1]]},
            }
            for c in root.claims
        ],
    }


def manifest_hash(root: Root) -> bytes:
    """sha256 of the canonical JSON; the value `post_root` carries."""
    canonical = json.dumps(root_to_json(root), sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).digest()


def state_to_json(state: EngineState) -> dict:
    return {
        "distributor_id": state.distributor_id.hex(),
        "root_id": state.root_id,
        "height": state.height,
        "accrued": {
            cid.hex(): {ph.hex(): [x.numerator, x.denominator] for ph, x in sorted(v.items())}
            for cid, v in sorted(state.accrued.items())
        },
        "link_count": state.link_count,
        "links_hash": state.links_hash.hex(),
    }


def state_from_json(data: dict) -> EngineState:
    h = bytes.fromhex
    return EngineState(
        distributor_id=h(data["distributor_id"]),
        root_id=data["root_id"],
        height=data["height"],
        accrued={
            h(cid): {h(ph): Fraction(n, d) for ph, (n, d) in v.items()}
            for cid, v in data["accrued"].items()
        },
        link_count=data.get("link_count", 0),
        links_hash=h(data.get("links_hash", EMPTY_LOG_HASH.hex())),
    )
