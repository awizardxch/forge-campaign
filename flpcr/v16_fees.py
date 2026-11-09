"""Fee events of a Forge V16 pool (protocol 17), read from the pool's own spends.

A V16 pool is a singleton whose inner puzzle is the slot-machine action layer
curried with the pool's whole state (`ACTION_LAYER.curry(finalizer,
merkle_root, state)`), and whose solution names the leaves it ran. So every
spent pool coin tells, from chain alone:

  * the state the pool was in BEFORE the spend: the reserves curried into the
    coin's puzzle reveal, which consensus has already bound to the coin's
    puzzle hash. This is the "pre-spend" state the oracle prices in the
    prologue, the same for every action of the spend;
  * every action the spend ran, in order. The action layer's solution is
    `(puzzles, selectors_and_proofs, solutions)`: `puzzles` lists each
    DISTINCT leaf once, `solutions` has one entry per action, so each action's
    leaf is resolved through its selector (2, 5, 11, ... over `puzzles` in
    order of first use; `selectors_and_proofs` is in reverse execution order),
    never by zipping `puzzles` with `solutions` (which drops a repeated leaf:
    forge-puzzles `forge_v16_resync.replay_spend`, found on testnet11 at
    4,693,721).

Each `swap` action yields one fee event. The LP fee is the part of the input the
curve does not credit (`exact_swap_output`): `gross - floor(gross * (10000 -
fee_bps) / 10000)`, in the input asset. It is valued in the pool's asset 0 at
the pre-spend spot price, `(r0 / w0) / (ri / wi)`, as an exact `Fraction`. A
V16 pool's asset ids are strictly ascending, so XCH (the zero id) is always
asset 0 when the pool holds it, and a fee paid in XCH is its mojos. A CAT/CAT
pool's fees are valued in its own asset 0: a campaign's fee weights are only
ever compared with each other, inside one pool (SPEC section 6.3), so one pool's
fixed numeraire gives the same shares XCH would at a constant rate, without
reading a second pool's price.

Protocol scope: only V16 (protocol 17) is read. The leaf, action layer and LP
TAIL hashes below are pinned from awizardxch/forge-puzzles at
`FORGE_PUZZLES_COMMIT` (`contracts/v16/compiled/*.rue.hash`); the parse is a
minimal reimplementation of `forge_v16_resync.replay_spend`'s selector walk, so
the engine needs no chia-blockchain and no forge-puzzles checkout at run time.
A pool whose LP asset is not the V16 TAIL curried with its launcher id and
protocol 17 is refused (`UnsupportedPool`); no V15 or V14 pool is read.

`PoolFeeWalk` follows one pool's singleton from its launcher, spend by spend,
through any `Rpc` (a full node or coinset.org; tests use recorded spends). It is
incremental: `to_json` keeps every spend it has read, and `update` reads only
what is new.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from chia_wallet_sdk import Clvm, Coin, curry_tree_hash

from .lp_crawl import Rpc

# --- pins --------------------------------------------------------------------------------

# awizardxch/forge-puzzles, the commit every hash below was read from.
FORGE_PUZZLES_COMMIT = "37f31991aabccd8421e607085fc62a522f0bbeed"
PROTOCOL_V16 = 17
SINGLETON_TOP_LAYER_V1_1_HASH = bytes.fromhex("7faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9f")
SINGLETON_LAUNCHER_HASH = bytes.fromhex("eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9")
# contracts/v16/compiled/<name>.rue.hash
ACTION_LAYER_HASH = bytes.fromhex("afa03f2903f9e4a293523237799074b13ab2361f250df10de5df884b0b09a22b")
LP_TAIL_MOD_HASH = bytes.fromhex("20624682bd638b07de7b77e0554e099514c11083d47ee9e18453b62907f5edfc")
LEAF_MOD_HASHES = {
    "forge_action_swap": bytes.fromhex("3fae54d0427d589eaf0c1d435933d584cf50f4f5ffb63e8ebbef3d0c2b96329f"),
    "forge_action_add": bytes.fromhex("c68ddf7b8727e3f21f5f73689721a911fe9746a095816ade8689ff4c0f169e71"),
    "forge_action_remove": bytes.fromhex("c6c8a0ad9da1b937948d50ea8113b2ba672897175279b7000bac836a6ca3883a"),
    "forge_action_observe": bytes.fromhex("49abebf9d3f11d374e6cd8ee306adb496febea1a3ac6fb5a40dc87a780cf77ff"),
    "forge_action_collect": bytes.fromhex("a07ccf201ff84778c5c235aed49979535d48ae4f28a67ab67a79b0a07a79c91a"),
    "forge_action_dao_fee": bytes.fromhex("55784b2239f2eb04631c580e1c9a1b12f5a2895ba34f2d3156740fcda196cd86"),
}
LEAF_NAMES = {h: name for name, h in LEAF_MOD_HASHES.items()}
WEIGHT_SCALE = 10_000  # forge_curve.rue
SINGLE_LEAF_SELECTOR = 2  # the first puzzle in `puzzles` is path 2, then 5, 11, ...


class FeeReadError(Exception):
    """The chain does not read as a V16 pool's spends."""


class UnsupportedPool(FeeReadError):
    """A pool this reader does not serve: anything but Forge V16 (protocol 17)."""


def _hex(value: str) -> bytes:
    return bytes.fromhex(value.removeprefix("0x"))


def v16_lp_asset_id(launcher_id: bytes, clvm: Optional[Clvm] = None) -> bytes:
    """The LP asset id a V16 pool with this launcher has: the V16 LP TAIL
    curried with (launcher id, 17)."""
    clvm = clvm or Clvm()
    return curry_tree_hash(LP_TAIL_MOD_HASH, [clvm.alloc(launcher_id).tree_hash(), clvm.alloc(PROTOCOL_V16).tree_hash()])


def check_protocol(launcher_id: bytes, lp_asset_id: bytes, declared: Optional[int] = None) -> None:
    """Refuse a pool that is not V16. `declared` is what the Forge responder
    says (`poolVersion`); the LP asset id is checked against the chain's own
    derivation whatever it says."""
    if declared is not None and declared < PROTOCOL_V16:
        raise UnsupportedPool(f"pool {launcher_id.hex()[:12]} is protocol {declared}: only Forge V16 (protocol 17) "
                              "and newer pools are supported")
    if declared is not None and declared > PROTOCOL_V16:
        raise UnsupportedPool(f"pool {launcher_id.hex()[:12]} is protocol {declared}: no fee reader is pinned for it yet "
                              f"(this one reads protocol {PROTOCOL_V16}, forge-puzzles {FORGE_PUZZLES_COMMIT[:7]})")
    if v16_lp_asset_id(launcher_id) != lp_asset_id:
        raise UnsupportedPool(f"pool {launcher_id.hex()[:12]}: LP asset {lp_asset_id.hex()[:12]} is not the V16 LP TAIL "
                              "for this launcher, so it is not a Forge V16 (protocol 17) pool")


def lp_fee(gross_input: int, fee_bps: int) -> int:
    """What a swap of `gross_input` leaves in the pool for its LPs (exact_swap_output)."""
    return gross_input - gross_input * (WEIGHT_SCALE - fee_bps) // WEIGHT_SCALE


def value_in_asset0(amount: int, asset: int, reserves: List[int], weights: List[int]) -> Fraction:
    """`amount` of asset `asset`, in asset 0 at the spot of `reserves`:
    amount * (r0 / w0) / (ri / wi), exact."""
    if asset == 0:
        return Fraction(amount)
    return Fraction(amount * reserves[0] * weights[asset], reserves[asset] * weights[0])


@dataclass(frozen=True)
class Swap:
    height: int  # the block the spend was included in
    asset_in: int
    asset_out: int
    gross_input: int
    lp_fee: int  # mojos of asset_in
    weight: Fraction  # lp_fee in asset 0 (XCH when the pool holds it) at the pre-spend spot

    def to_json(self) -> list:
        return [self.height, self.asset_in, self.asset_out, self.gross_input, self.lp_fee,
                [self.weight.numerator, self.weight.denominator]]

    @classmethod
    def from_json(cls, x: list) -> "Swap":
        return cls(x[0], x[1], x[2], x[3], x[4], Fraction(x[5][0], x[5][1]))


def _ints(p) -> List[int]:
    return [x.to_int() for x in p.to_list()]


def parse_pool_spend(launcher_id: bytes, puzzle_reveal: bytes, solution: bytes, height: int,
                     clvm: Optional[Clvm] = None) -> Tuple[List[str], List[Swap]]:
    """The leaves one spend of a V16 pool coin ran, in execution order, and a
    `Swap` for each swap among them."""
    clvm = clvm or Clvm()
    reveal = clvm.deserialize(puzzle_reveal)
    top = reveal.uncurry()
    if top is None or top.program.tree_hash() != SINGLETON_TOP_LAYER_V1_1_HASH:
        raise FeeReadError("the pool coin is not a singleton")
    struct, inner = top.args
    if struct.rest().first().to_atom() != launcher_id:
        raise FeeReadError(f"the coin is another singleton's, not pool {launcher_id.hex()[:12]}'s")
    layer = inner.uncurry()
    if layer is None or layer.program.tree_hash() != ACTION_LAYER_HASH:
        raise UnsupportedPool(f"pool {launcher_id.hex()[:12]} is not on the V16 action layer")
    _finalizer, _root, state = layer.args
    reserves = _ints(state.first())

    inner_solution = clvm.deserialize(solution).to_list()[2]
    puzzles, selectors, solutions = inner_solution.to_list()[:3]
    puzzle_list = puzzles.to_list()
    selector_list = [entry.first().to_int() for entry in selectors.to_list()][::-1]
    solution_list = solutions.to_list()
    if len(selector_list) != len(solution_list):
        raise FeeReadError("a pool spend has a selector count its solutions do not match")

    def leaf_for(selector: int):
        index, value = 0, SINGLE_LEAF_SELECTOR
        while value != selector:
            value, index = value * 2 + 1, index + 1
            if index >= len(puzzle_list):
                raise FeeReadError("a pool spend names a leaf selector it does not reveal")
        return puzzle_list[index]

    names, swaps = [], []
    for selector, leaf_solution in zip(selector_list, solution_list):
        leaf = leaf_for(selector).uncurry()
        name = LEAF_NAMES.get(leaf.program.tree_hash()) if leaf is not None else None
        if name is None:
            raise UnsupportedPool(f"pool {launcher_id.hex()[:12]} ran a leaf that is not a V16 leaf")
        names.append(name)
        if name != "forge_action_swap":
            continue
        config = leaf.args[0].to_list()
        weights, fee_bps = _ints(config[1]), config[2].to_int()
        # [h, birth, asset_in, asset_out, gross_input, claimed_output, settlement_parent, settlement_amount]
        args = leaf_solution.to_list()
        asset_in, asset_out, gross = args[2].to_int(), args[3].to_int(), args[4].to_int()
        fee = lp_fee(gross, fee_bps)
        swaps.append(Swap(height, asset_in, asset_out, gross, fee, value_in_asset0(fee, asset_in, reserves, weights)))
    return names, swaps


@dataclass
class PoolFeeWalk:
    """One V16 pool's swaps, from its launcher to the last spend read."""

    launcher_id: bytes
    tip: Optional[bytes] = None  # the last pool coin found; unspent when last read
    tip_created: int = 0
    swaps: List[Swap] = field(default_factory=list)
    spends: int = 0  # pool spends read
    calls: int = 0

    def update(self, rpc: Rpc, clvm: Optional[Clvm] = None) -> int:
        """Read every pool spend since the last update; returns how many."""
        clvm = clvm or Clvm()
        read = 0
        if self.tip is None:
            eve = self._odd_child(rpc, self.launcher_id)
            if eve is None:
                return 0
            self.tip, self.tip_created = eve
        while True:
            rec = rpc("get_coin_record_by_name", {"name": "0x" + self.tip.hex()})
            self.calls += 1
            record = rec.get("coin_record")
            if not record:
                raise FeeReadError(f"pool coin {self.tip.hex()[:12]} is unknown to the node")
            if not record.get("spent"):
                return read
            height = int(record["spent_block_index"])
            out = rpc("get_puzzle_and_solution", {"coin_id": "0x" + self.tip.hex(), "height": height})["coin_solution"]
            self.calls += 1
            reveal = _hex(out["puzzle_reveal"])
            if clvm.deserialize(reveal).tree_hash() != _hex(record["coin"]["puzzle_hash"]):
                raise FeeReadError(f"the reveal of pool coin {self.tip.hex()[:12]} does not hash to its puzzle hash")
            _, swaps = parse_pool_spend(self.launcher_id, reveal, _hex(out["solution"]), height, clvm)
            self.swaps.extend(swaps)
            self.spends += 1
            read += 1
            nxt = self._odd_child(rpc, self.tip)
            if nxt is None:  # melted: the pool is gone
                return read
            self.tip, self.tip_created = nxt

    def _odd_child(self, rpc: Rpc, parent: bytes) -> Optional[Tuple[bytes, int]]:
        """The singleton's successor: the top layer allows exactly one odd child."""
        out = rpc("get_coin_records_by_parent_ids", {"parent_ids": ["0x" + parent.hex()], "include_spent_coins": True})
        self.calls += 1
        odd = [r for r in out.get("coin_records") or [] if int(r["coin"]["amount"]) % 2 == 1]
        if not odd:
            return None
        if len(odd) > 1:
            raise FeeReadError(f"coin {parent.hex()[:12]} has more than one odd child; not a singleton")
        c = odd[0]["coin"]
        coin = Coin(_hex(c["parent_coin_info"]), _hex(c["puzzle_hash"]), int(c["amount"]))
        return coin.coin_id(), int(odd[0].get("confirmed_block_index") or 0)

    def events(self, start: int, end: int) -> List[Tuple[int, Fraction]]:
        """(height, weight) for each swap in [start, end)."""
        return [(s.height, s.weight) for s in self.swaps if start <= s.height < end and s.weight > 0]

    def to_json(self) -> dict:
        return {"launcher_id": self.launcher_id.hex(), "tip": self.tip.hex() if self.tip else None,
                "tip_created": self.tip_created, "spends": self.spends, "swaps": [s.to_json() for s in self.swaps],
                "forge_puzzles": FORGE_PUZZLES_COMMIT}

    @classmethod
    def from_json(cls, data: dict) -> "PoolFeeWalk":
        if data.get("forge_puzzles") != FORGE_PUZZLES_COMMIT:
            # Read under other pins: start over rather than mix two readers' weights.
            return cls(bytes.fromhex(data["launcher_id"]))
        return cls(bytes.fromhex(data["launcher_id"]), bytes.fromhex(data["tip"]) if data.get("tip") else None,
                   data.get("tip_created", 0), [Swap.from_json(x) for x in data.get("swaps", [])], data.get("spends", 0))
