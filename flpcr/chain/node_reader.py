"""`nfts.ChainReader` over a full node or coinset.org: the same three calls the
simulator answers, read from a live chain. With it, `ChainNftSource` reads a
real collection: every NFT its DID minted, and every owner each one has had.

Answers are cached: a spent coin never changes, and an unspent coin is
re-read only when asked again after `refresh`.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from chia_wallet_sdk import Coin, CoinSpend, CoinState

from ..lp_crawl import Rpc, node_rpc


def _hex(value: str) -> bytes:
    return bytes.fromhex(value.removeprefix("0x"))


def _state(rec: dict) -> CoinState:
    c = rec["coin"]
    coin = Coin(_hex(c["parent_coin_info"]), _hex(c["puzzle_hash"]), int(c["amount"]))
    spent = int(rec.get("spent_block_index") or 0) if rec.get("spent") else 0
    return CoinState(coin, spent or None, int(rec.get("confirmed_block_index") or 0))


class NodeReader:
    def __init__(self, rpc: Rpc):
        self.rpc = rpc
        self.calls = 0
        self._states: Dict[bytes, Optional[CoinState]] = {}
        self._children: Dict[bytes, List[CoinState]] = {}
        self._spends: Dict[bytes, Optional[CoinSpend]] = {}

    @classmethod
    def connect(cls, url: str) -> "NodeReader":
        return cls(node_rpc(url))

    def refresh(self) -> None:
        """Forget what may still change: unspent coins and their (empty) child lists."""
        for k in [k for k, s in self._states.items() if s is None or s.spent_height is None]:
            self._states.pop(k, None)
            self._children.pop(k, None)

    def _call(self, endpoint: str, body: dict) -> dict:
        self.calls += 1
        return self.rpc(endpoint, body)

    def coin_state(self, coin_id: bytes) -> Optional[CoinState]:
        if coin_id not in self._states:
            try:
                rec = self._call("get_coin_record_by_name", {"name": "0x" + coin_id.hex()}).get("coin_record")
            except RuntimeError:  # coinset answers "not found" as an error
                rec = None
            self._states[coin_id] = _state(rec) if rec else None
        return self._states[coin_id]

    def children(self, coin_id: bytes) -> List[CoinState]:
        if coin_id not in self._children:
            out = self._call("get_coin_records_by_parent_ids",
                             {"parent_ids": ["0x" + coin_id.hex()], "include_spent_coins": True})
            kids = [_state(r) for r in out.get("coin_records") or []]
            for k in kids:
                self._states.setdefault(k.coin.coin_id(), k)
            self._children[coin_id] = kids
        return self._children[coin_id]

    def coin_spend(self, coin_id: bytes) -> Optional[CoinSpend]:
        if coin_id not in self._spends:
            state = self.coin_state(coin_id)
            if state is None or state.spent_height is None:
                return None
            sol = self._call("get_puzzle_and_solution",
                             {"coin_id": "0x" + coin_id.hex(), "height": state.spent_height})["coin_solution"]
            self._spends[coin_id] = CoinSpend(state.coin, _hex(sol["puzzle_reveal"]), _hex(sol["solution"]))
        return self._spends[coin_id]
