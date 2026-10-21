"""Every holder of a Forge pool's LP CAT, read from the chain.

Each V16 LP mint goes through an eve coin at one fixed puzzle hash per pool:
the LP CAT wrapping the pinned mint inner puzzle. The crawl starts from that
puzzle hash's whole coin history and walks forward, coin by coin: a spent LP
coin's own spend says which inner puzzle hash each child LP coin belongs to
(`Puzzle.parse_child_cats`). Every LP coin then becomes two balance events for
its inner puzzle hash: +amount at the height it was created, -amount at the
height it was spent.

Inner puzzle hashes that only hold LP in passing are custody, not positions:
the mint and melt inner puzzles, the offer settlement puzzle, and the burn
address (the pool's locked floor). They are excluded from the events.

The crawl is incremental: `LpCrawl.to_json` keeps every coin it has seen, and a
later `update` only reads coins created or spent since.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set

from chia_wallet_sdk import Clvm, Coin, cat_puzzle_hash

from .sources import HolderEvent

# Pinned by the V16 build (chia-cfmm forge_v16_driver: LP_MINT_INNER, LP_MELT_INNER,
# OFFER_MOD_HASH, BURN_PUZZLE_HASH). The same for every V16 pool.
LP_MINT_INNER_HASH = bytes.fromhex("66811aedacdefe1228ecf5c9553e02627173b81db5f8d90ca44f61297bdfc4c6")
LP_MELT_INNER_HASH = bytes.fromhex("8b5b948a4c0e713bcb0b28ddb85cc1609d799b40c6fb65b2372de96976ad277a")
OFFER_MOD_HASH = bytes.fromhex("cfbfdeed5c4ca2de3d0bf520b9cb4bb7743a359bd2e6a188d19ce7dffc21d3e7")
BURN_PUZZLE_HASH = bytes.fromhex("000000000000000000000000000000000000000000000000000000000000dead")
CUSTODY = frozenset({LP_MINT_INNER_HASH, LP_MELT_INNER_HASH, OFFER_MOD_HASH, BURN_PUZZLE_HASH})

Rpc = Callable[[str, dict], dict]


def node_rpc(url: str) -> Rpc:
    def call(endpoint: str, body: dict) -> dict:
        req = urllib.request.Request(f"{url.rstrip('/')}/{endpoint}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "flpcr-crawl/1.0"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.loads(resp.read().decode())
        if not out.get("success", True):
            raise RuntimeError(f"{endpoint}: {out.get('error')}")
        return out
    return call


def _hex(value: str) -> bytes:
    return bytes.fromhex(value.removeprefix("0x"))


@dataclass
class LpCoin:
    coin_id: bytes
    parent: bytes
    puzzle_hash: bytes
    amount: int
    inner_ph: Optional[bytes]  # None while unknown (an eve's own coin is custody)
    created: int
    spent: int  # 0 while unspent

    def to_json(self) -> list:
        return [self.coin_id.hex(), self.parent.hex(), self.puzzle_hash.hex(), self.amount,
                self.inner_ph.hex() if self.inner_ph else None, self.created, self.spent]

    @classmethod
    def from_json(cls, x: list) -> "LpCoin":
        return cls(bytes.fromhex(x[0]), bytes.fromhex(x[1]), bytes.fromhex(x[2]), x[3],
                   bytes.fromhex(x[4]) if x[4] else None, x[5], x[6])


@dataclass
class LpCrawl:
    """The LP coins of one asset, as far as the chain has been read."""

    asset_id: bytes
    coins: Dict[bytes, LpCoin] = field(default_factory=dict)
    expanded: Set[bytes] = field(default_factory=set)  # spent coins whose children are read
    calls: int = 0

    def eve_puzzle_hash(self) -> bytes:
        return cat_puzzle_hash(self.asset_id, LP_MINT_INNER_HASH)

    def _record(self, rec: dict, inner_ph: Optional[bytes]) -> LpCoin:
        c = rec["coin"]
        coin = LpCoin(Coin(_hex(c["parent_coin_info"]), _hex(c["puzzle_hash"]), int(c["amount"])).coin_id(),
                      _hex(c["parent_coin_info"]), _hex(c["puzzle_hash"]), int(c["amount"]), inner_ph,
                      int(rec.get("confirmed_block_index") or 0),
                      int(rec.get("spent_block_index") or 0) if rec.get("spent") else 0)
        old = self.coins.get(coin.coin_id)
        if old is not None and old.inner_ph is not None and coin.inner_ph is None:
            coin.inner_ph = old.inner_ph
        self.coins[coin.coin_id] = coin
        return coin

    def update(self, rpc: Rpc, clvm: Optional[Clvm] = None, batch: int = 100) -> int:
        """Read what is new on chain; returns how many coins were added or changed."""
        clvm = clvm or Clvm()
        before = {k: (v.spent, v.inner_ph) for k, v in self.coins.items()}
        eve_ph = self.eve_puzzle_hash()
        out = rpc("get_coin_records_by_puzzle_hash", {"puzzle_hash": "0x" + eve_ph.hex(), "include_spent_coins": True})
        self.calls += 1
        for rec in out.get("coin_records") or []:
            self._record(rec, LP_MINT_INNER_HASH)
        # Refresh coins we hold as unspent: they may have been spent since.
        live = [k for k, v in self.coins.items() if not v.spent]
        for i in range(0, len(live), batch):
            out = rpc("get_coin_records_by_names", {"names": ["0x" + k.hex() for k in live[i:i + batch]],
                                                     "include_spent_coins": True})
            self.calls += 1
            for rec in out.get("coin_records") or []:
                old = self.coins.get(Coin(_hex(rec["coin"]["parent_coin_info"]), _hex(rec["coin"]["puzzle_hash"]),
                                          int(rec["coin"]["amount"])).coin_id())
                self._record(rec, old.inner_ph if old else None)
        # Walk forward from every spent coin not yet expanded.
        while True:
            todo = [v for k, v in self.coins.items() if v.spent and k not in self.expanded]
            if not todo:
                break
            for coin in todo:
                self._expand(rpc, clvm, coin)
        return sum(1 for k, v in self.coins.items() if before.get(k) != (v.spent, v.inner_ph))

    def _expand(self, rpc: Rpc, clvm: Clvm, coin: LpCoin) -> None:
        """Read `coin`'s spend and record its LP children with their inner puzzle hashes."""
        spend = rpc("get_puzzle_and_solution", {"coin_id": "0x" + coin.coin_id.hex(), "height": coin.spent})["coin_solution"]
        self.calls += 1
        puzzle = clvm.deserialize(_hex(spend["puzzle_reveal"])).puzzle()
        solution = clvm.deserialize(_hex(spend["solution"]))
        parent = Coin(coin.parent, coin.puzzle_hash, coin.amount)
        children = puzzle.parse_child_cats(parent, solution) or []
        inner_of = {}
        for cat in children:
            inner_of[cat.coin.coin_id()] = cat.info.p2_puzzle_hash
        kids = rpc("get_coin_records_by_parent_ids", {"parent_ids": ["0x" + coin.coin_id.hex()], "include_spent_coins": True})
        self.calls += 1
        for rec in kids.get("coin_records") or []:
            c = rec["coin"]
            cid = Coin(_hex(c["parent_coin_info"]), _hex(c["puzzle_hash"]), int(c["amount"])).coin_id()
            if cid in inner_of:  # an LP child (a melt leaves none; XCH change is not ours)
                self._record(rec, inner_of[cid])
        self.expanded.add(coin.coin_id)

    def events(self, before: Optional[int] = None) -> List[HolderEvent]:
        """Balance changes of every non-custody holder, in height order."""
        out = []
        for c in self.coins.values():
            if c.inner_ph is None or c.inner_ph in CUSTODY:
                continue
            if before is None or c.created < before:
                out.append(HolderEvent(c.created, c.inner_ph, c.amount))
            if c.spent and (before is None or c.spent < before):
                out.append(HolderEvent(c.spent, c.inner_ph, -c.amount))
        return sorted(out, key=lambda e: e.height)

    def balances(self, at: Optional[int] = None) -> Dict[bytes, int]:
        bal: Dict[bytes, int] = {}
        for e in self.events(None if at is None else at + 1):
            bal[e.puzzle_hash] = bal.get(e.puzzle_hash, 0) + e.delta
        return {k: v for k, v in bal.items() if v}

    def to_json(self) -> dict:
        return {"asset_id": self.asset_id.hex(), "coins": [c.to_json() for c in self.coins.values()],
                "expanded": sorted(k.hex() for k in self.expanded)}

    @classmethod
    def from_json(cls, data: dict) -> "LpCrawl":
        crawl = cls(bytes.fromhex(data["asset_id"]))
        for x in data.get("coins", []):
            c = LpCoin.from_json(x)
            crawl.coins[c.coin_id] = c
        crawl.expanded = {bytes.fromhex(k) for k in data.get("expanded", [])}
        return crawl


def holders_of(crawls: Iterable[LpCrawl]) -> Set[bytes]:
    return {e.puzzle_hash for crawl in crawls for e in crawl.events()}
