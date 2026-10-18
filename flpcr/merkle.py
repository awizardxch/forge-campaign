"""The claim tree.

Same construction as chia's `merkle_utils` (leaf prefix 0x01, node prefix 0x02,
midpoint split, proof = (path bitmask, sibling hashes bottom-up)), which is also
what `merkle_utils.clib`'s `simplify_merkle_proof` checks on chain. The leaves
are tree hashes of claim entries (see `claim_leaf`).
"""

from __future__ import annotations

from hashlib import sha256
from typing import Dict, List, Sequence, Tuple

from .clvm import sha256tree

LEAF_PREFIX = b"\x01"
NODE_PREFIX = b"\x02"
CLAIM_TAG = "flpcr-claim-v1"

Proof = Tuple[int, List[bytes]]


def claim_leaf(distributor_id: bytes, campaign_id: bytes, puzzle_hash: bytes, cumulative: int) -> bytes:
    """The value the puzzle tree-hashes before checking the proof.

    Cumulative, as in Merkl: the same holder appears in every later root with a
    larger number, and the distributor pays the difference from what its claim
    slot records. `distributor_id` keeps a leaf from verifying anywhere else;
    `campaign_id` is there because claims are tracked per campaign, which is
    what lets the creator reclaim exactly what holders left unclaimed.
    """
    return sha256tree([CLAIM_TAG, distributor_id, campaign_id, puzzle_hash, cumulative])


def _split(items: Sequence[bytes]):
    if len(items) == 1:
        return items[0]
    mid = (len(items) + 1) >> 1
    return (_split(items[:mid]), _split(items[mid:]))


def _build(node) -> Tuple[bytes, Dict[bytes, Proof]]:
    if isinstance(node, bytes):
        return sha256(LEAF_PREFIX + node).digest(), {node: (0, [])}
    left_root, left = _build(node[0])
    right_root, right = _build(node[1])
    proofs: Dict[bytes, Proof] = {}
    for leaf, (path, hashes) in left.items():
        proofs[leaf] = (path, hashes + [right_root])
    for leaf, (path, hashes) in right.items():
        proofs[leaf] = (path | (1 << len(hashes)), hashes + [left_root])
    return sha256(NODE_PREFIX + left_root + right_root).digest(), proofs


def build_tree(leaves: Sequence[bytes]) -> Tuple[bytes, Dict[bytes, Proof]]:
    """Root and per-leaf proofs, leaves in the order given. Leaves must be distinct."""
    if not leaves:
        raise ValueError("a root with no claims is not posted")
    if len(set(leaves)) != len(leaves):
        raise ValueError("duplicate leaf")
    return _build(_split(list(leaves)))


def root_from_proof(leaf: bytes, proof: Proof) -> bytes:
    path, hashes = proof
    acc = sha256(LEAF_PREFIX + leaf).digest()
    for sibling in hashes:
        if path & 1:
            acc = sha256(NODE_PREFIX + sibling + acc).digest()
        else:
            acc = sha256(NODE_PREFIX + acc + sibling).digest()
        path >>= 1
    return acc
