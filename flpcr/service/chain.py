"""What the hosted service needs from a chain, behind one interface.

`NodeChain` talks to a full node or coinset.org. `SimChain` wraps the wallet
SDK's simulator, so the service's tests push exactly the bundles it would push
on testnet or mainnet: signed by its own keys, with the claimants' signatures
aggregated in.

`peak()` is the height a spend may name: the last *transaction* block, not the
peak (a spend naming a non-transaction height stays pending, see Forge's
"spend height is the last tx block").
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import List, Optional, Sequence

from chia_wallet_sdk import Coin, CoinSpend, Signature, SpendBundle

from ..chain.node_reader import NodeReader


def bundle_json(spends: Sequence[CoinSpend], signature: Signature) -> dict:
    return {
        "coin_spends": [{"coin": {"parent_coin_info": "0x" + s.coin.parent_coin_info.hex(),
                                  "puzzle_hash": "0x" + s.coin.puzzle_hash.hex(), "amount": s.coin.amount},
                         "puzzle_reveal": "0x" + s.puzzle_reveal.hex(), "solution": "0x" + s.solution.hex()}
                        for s in spends],
        "aggregated_signature": "0x" + signature.to_bytes().hex(),
    }


class PushRefused(RuntimeError):
    pass


class NodeChain:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.reader = NodeReader(self.rpc)

    def rpc(self, endpoint: str, body: dict, attempts: int = 3) -> dict:
        req = urllib.request.Request(f"{self.url}/{endpoint}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "flpcr-service/1.0"},
                                     method="POST")
        tries = 1 if endpoint == "push_tx" else attempts
        for attempt in range(tries):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    out = json.loads(resp.read().decode())
                if not out.get("success", True) and endpoint != "push_tx":
                    raise RuntimeError(f"{endpoint}: {out.get('error')}")
                return out
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt + 1 >= tries:
                    raise
                time.sleep(5 * (attempt + 1))
        raise RuntimeError("unreachable")

    def peak(self) -> int:
        block = self.rpc("get_blockchain_state", {})["blockchain_state"]["peak"]
        if block.get("timestamp") is not None:  # a transaction block
            return int(block["height"])
        return int(block["prev_transaction_block_height"])

    def push(self, spends: Sequence[CoinSpend], signature: Signature) -> dict:
        verdict = self.rpc("push_tx", {"spend_bundle": bundle_json(spends, signature)})
        if not verdict.get("success"):
            raise PushRefused(str(verdict.get("error") or verdict))
        return verdict

    def confirmed_at(self, coin_id: bytes) -> Optional[int]:
        state = self.reader.coin_state(coin_id)
        self.reader.refresh()
        return state.created_height if state is not None else None

    def unspent_at(self, puzzle_hashes: Sequence[bytes]) -> List[Coin]:
        out = self.rpc("get_coin_records_by_puzzle_hashes",
                       {"puzzle_hashes": ["0x" + ph.hex() for ph in puzzle_hashes], "include_spent_coins": False})
        coins = []
        for r in out.get("coin_records") or []:
            c = r["coin"]
            coins.append(Coin(bytes.fromhex(c["parent_coin_info"][2:]), bytes.fromhex(c["puzzle_hash"][2:]), int(c["amount"])))
        return coins


class SimChain:
    """The simulator as a chain: every push is a block."""

    def __init__(self, sim):
        self.sim = sim
        self.reader = sim

    def peak(self) -> int:
        return self.sim.height()

    def push(self, spends: Sequence[CoinSpend], signature: Signature) -> dict:
        try:
            self.sim.new_transaction(SpendBundle(list(spends), signature))
        except ValueError as e:
            raise PushRefused(str(e)) from e
        return {"success": True}

    def confirmed_at(self, coin_id: bytes) -> Optional[int]:
        state = self.sim.coin_state(coin_id)
        return state.created_height if state is not None else None

    def unspent_at(self, puzzle_hashes: Sequence[bytes]) -> List[Coin]:
        return [coin for ph in puzzle_hashes for coin in self.sim.unspent_coins(ph, False)]
