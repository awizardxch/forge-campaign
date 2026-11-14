"""Network fee for a bundle, by the rule Forge's router uses (Forge PR 307).

Chia refuses a NONZERO fee under 5 mojos per unit of cost only while the mempool is at full
capacity (`mempool_manager.py`: the INVALID_FEE_TOO_CLOSE_TO_ZERO check sits under
`if self.mempool.at_full_capacity(cost)`). Below that the node takes any fee, zero included.
So: while the mempool has room (the router's line: under 70% of its maximum cost, this bundle
included) the fee is 0; otherwise the floor rate plus 10%, never a mojo under the cliff.
No reading of the mempool counts as full.
"""

from __future__ import annotations

from typing import Optional

FLOOR_PER_COST = 5      # mojos per unit of cost, the full-mempool floor
MARGIN_NUM, MARGIN_DEN = 11, 10  # +10%, as the router pays
ROOM = 0.70             # under this share of the mempool's maximum cost, the mempool has room


def has_room(cost: int, mempool_cost: Optional[int], mempool_max: Optional[int]) -> bool:
    if not mempool_max or mempool_cost is None:
        return False
    return mempool_cost + cost <= ROOM * mempool_max


def bundle_fee(cost: int, mempool_cost: Optional[int], mempool_max: Optional[int]) -> int:
    """Mojos of fee for a bundle of `cost`: 0 with room, else ceil(cost x 5 x 1.1)."""
    if cost < 0:
        raise ValueError("cost is never negative")
    if has_room(cost, mempool_cost, mempool_max):
        return 0
    return -(-cost * FLOOR_PER_COST * MARGIN_NUM // MARGIN_DEN)


def bundle_fee_from_node(cost: int, blockchain_state: dict) -> int:
    """`bundle_fee` from a full node's `get_blockchain_state` answer (`blockchain_state`)."""
    return bundle_fee(cost, blockchain_state.get("mempool_cost"), blockchain_state.get("mempool_max_total_cost"))
