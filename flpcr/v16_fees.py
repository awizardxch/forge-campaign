"""Fee events of a Forge V16 (protocol 17), V17 (protocol 18) or V18 (protocol 19) pool, read from the pool's own spends.

V17 (2026-11-13): a V17 pool is the same shape, on the same action layer, with eight leaves
(V16's six plus `rebase` and `refund`, the rCAT leaves) and its own LP TAIL; its swap leaf takes
the same solution and the curve's `exact_swap_output` is unchanged, so its fees read the same
way. An rCAT reserve is an amount in state like any other. Each revision's pins are in
`REVISIONS`; a spend is read by the revision its leaves belong to. The module keeps its V16 name
because the engine's cache and the fixtures name it.

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

Protocol scope: V16 (protocol 17), V17 (protocol 18) and V18 (protocol 19; pinned from Forge contracts/v18/compiled at `FORGE_V18_COMMIT`). The leaf, action layer and LP
TAIL hashes below are pinned from awizardxch/forge-puzzles at
`FORGE_PUZZLES_COMMIT` (`contracts/v16/compiled/*.rue.hash`); the parse is a
minimal reimplementation of `forge_v16_resync.replay_spend`'s selector walk, so
the engine needs no chia-blockchain and no forge-puzzles checkout at run time.
A pool whose LP asset is not the V16 TAIL curried with its launcher id and
protocol 17 is refused (`UnsupportedPool`); no V15 or V14 pool is read. The V17 pins are read
from awizardxch/Forge `projects/chia-cfmm/contracts/v17/compiled/*.rue.hash` at `FORGE_V17_COMMIT`
(V17 is not in forge-puzzles yet), and a V17 pool's LP asset must be the V17 TAIL curried with
(launcher id, 18).

Selectors are RUN on the revealed leaves, as the action layer runs them (audit 2026-11-11 CE-13,
FP-07): the puzzle enforces no selector form, so `(q . leaf)` is a valid spend.

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

# awizardxch/Forge (private), projects/chia-cfmm/contracts/v17/compiled/<name>.rue.hash at this commit.
FORGE_V17_COMMIT = "350d023498df3edab4635aa381743435fd344ce9"
PROTOCOL_V17 = 18
V17_LP_TAIL_MOD_HASH = bytes.fromhex("a98d5affa08310efedd0ac9eef702dadbf1119805719b722996edc7e9e36ed79")
V17_LEAF_MOD_HASHES = {
    "forge_action_swap": bytes.fromhex("31f7e3d18635f2757fda518a0f5b1e542d0e39edd77faa8e543df6a8e11d1181"),
    "forge_action_add": bytes.fromhex("46de1efcb499e0b4cafce23301de87531e85b88dcc9befac8e8140988b8f0131"),
    "forge_action_remove": bytes.fromhex("741250464a11df9336bb89eca5112b0803b4a79a5393969cae4a171e974ef164"),
    "forge_action_observe": bytes.fromhex("1a0813f06f4d34a947bf2b816fea73c8ebdc9f41afab8364589ea52c74fb095f"),
    "forge_action_collect": bytes.fromhex("19be84b32d07cd79e742fb41e3674c9efd4bb42620dfe5ff44994a8f080793a4"),
    "forge_action_dao_fee": bytes.fromhex("12b0e026b8146dc681f15e61ea4e25a4ff74043b120d555bb63186b20b4ec7ef"),
    "forge_action_rebase": bytes.fromhex("939facab91a53ce17b2d09d1234918f4d77db0c532120b2c316562263139a8e9"),
    "forge_action_refund": bytes.fromhex("4993423b5cec080efc24e418168fb6832401a83fead9d289b17f8d4514ba6313"),
}


# awizardxch/Forge (private), projects/chia-cfmm/contracts/v18/compiled/<name>.rue.hash at this commit.
# V18 (protocol 19) = V17 + one `register` rule (FP-R05b). Its LP TAIL is a new build, and the
# add/remove/rebase/refund leaves recompile with it (they curry the TAIL), so they are pinned again;
# swap, observe, collect and dao_fee are V17's own hashes. The LP custody puzzles (mint, melt) are
# unchanged, so the LP crawl reads V18 LP as it reads V16's and V17's.
FORGE_V18_COMMIT = "57cab8339649a2e425c34346ffcf5b8d31406e37"
PROTOCOL_V18 = 19
V18_LP_TAIL_MOD_HASH = bytes.fromhex("de05823e03ae186b7431f0618621884a72be5997723c7134919acaea39142c8f")
V18_LEAF_MOD_HASHES = {
    "forge_action_swap": bytes.fromhex("31f7e3d18635f2757fda518a0f5b1e542d0e39edd77faa8e543df6a8e11d1181"),
    "forge_action_add": bytes.fromhex("44bbab0c480fbb00bd7e1e6fd4fb08399ffa2df5846c9ebae84fa6aad7581dfa"),
    "forge_action_remove": bytes.fromhex("ea4787013cb0f795f37398f10b3cb83391eaeb2292a5ee2276cf2d0eb5929a6f"),
    "forge_action_observe": bytes.fromhex("1a0813f06f4d34a947bf2b816fea73c8ebdc9f41afab8364589ea52c74fb095f"),
    "forge_action_collect": bytes.fromhex("19be84b32d07cd79e742fb41e3674c9efd4bb42620dfe5ff44994a8f080793a4"),
    "forge_action_dao_fee": bytes.fromhex("12b0e026b8146dc681f15e61ea4e25a4ff74043b120d555bb63186b20b4ec7ef"),
    "forge_action_rebase": bytes.fromhex("2479dfab10ef6f2b209f9a133c2a9f98256507ac91ce9d23f0888c19fccfff09"),
    "forge_action_refund": bytes.fromhex("a80974593d68104932b72370571ff1ba0a432db096936f4b47c7e958bbb152ea"),
}


@dataclass(frozen=True)
class Revision:
    name: str
    protocol: int
    lp_tail_mod_hash: bytes
    leaves: Dict[str, bytes]
    source: str


# Adding a revision is one entry here. A revision that keeps the previous one's pool leaves and
# LP TAIL mod (V18 = V17 + a registry rule) reuses its dicts: the LP asset (TAIL curried with the
# protocol number) is what tells the two apart, and a leaf hash may belong to several revisions.
REVISIONS: Dict[int, Revision] = {
    PROTOCOL_V16: Revision("V16", PROTOCOL_V16, LP_TAIL_MOD_HASH, LEAF_MOD_HASHES,
                           f"forge-puzzles {FORGE_PUZZLES_COMMIT[:7]}"),
    PROTOCOL_V17: Revision("V17", PROTOCOL_V17, V17_LP_TAIL_MOD_HASH, V17_LEAF_MOD_HASHES,
                           f"Forge {FORGE_V17_COMMIT[:7]}"),
    PROTOCOL_V18: Revision("V18", PROTOCOL_V18, V18_LP_TAIL_MOD_HASH, V18_LEAF_MOD_HASHES,
                           f"Forge {FORGE_V18_COMMIT[:7]}"),
}
MIN_PROTOCOL, MAX_PROTOCOL = min(REVISIONS), max(REVISIONS)


def leaf_index(revisions: Dict[int, Revision]) -> Dict[bytes, Tuple[frozenset, str]]:
    """leaf mod hash -> (the protocols whose pools run it, leaf name)."""
    out: Dict[bytes, Tuple[frozenset, str]] = {}
    for rev in revisions.values():
        for name, h in rev.leaves.items():
            seen, _ = out.get(h, (frozenset(), name))
            out[h] = (seen | {rev.protocol}, name)
    return out


LEAVES_BY_HASH = leaf_index(REVISIONS)
# What a walk's cache was read under: a cache read under other pins starts over.
PINS_ID = "+".join(f"{p}:{r.source}" for p, r in sorted(REVISIONS.items()))
WEIGHT_SCALE = 10_000  # forge_curve.rue
SINGLE_LEAF_SELECTOR = 2  # the first puzzle in `puzzles` is path 2, then 5, 11, ...


class FeeReadError(Exception):
    """The chain does not read as a V16 pool's spends."""


class UnsupportedPool(FeeReadError):
    """A pool this reader does not serve: anything but Forge V16 (protocol 17) or V17 (protocol 18)."""


def _hex(value: str) -> bytes:
    return bytes.fromhex(value.removeprefix("0x"))


def lp_asset_id(launcher_id: bytes, protocol: int, clvm: Optional[Clvm] = None) -> bytes:
    """The LP asset id a pool of this revision with this launcher has: the revision's LP TAIL
    curried with (launcher id, protocol)."""
    clvm = clvm or Clvm()
    rev = REVISIONS[protocol]
    return curry_tree_hash(rev.lp_tail_mod_hash, [clvm.alloc(launcher_id).tree_hash(), clvm.alloc(protocol).tree_hash()])


def v16_lp_asset_id(launcher_id: bytes, clvm: Optional[Clvm] = None) -> bytes:
    """The LP asset id a V16 pool with this launcher has: the V16 LP TAIL
    curried with (launcher id, 17)."""
    return lp_asset_id(launcher_id, PROTOCOL_V16, clvm)


def v17_lp_asset_id(launcher_id: bytes, clvm: Optional[Clvm] = None) -> bytes:
    """The LP asset id a V17 pool with this launcher has: the V17 LP TAIL curried with (launcher id, 18)."""
    return lp_asset_id(launcher_id, PROTOCOL_V17, clvm)


def check_protocol(launcher_id: bytes, lp_asset_id_: bytes, declared: Optional[int] = None) -> int:
    """The pool's protocol, or `UnsupportedPool` for anything but V16 or V17. `declared` is what
    the Forge responder says (`poolVersion`); the LP asset id is checked against the chain's own
    derivation for that revision whatever it says (and, with nothing declared, against each)."""
    if declared is not None and declared < MIN_PROTOCOL:
        raise UnsupportedPool(f"pool {launcher_id.hex()[:12]} is protocol {declared}: only Forge V16 (protocol 17) "
                              "and newer pools are supported")
    if declared is not None and declared > MAX_PROTOCOL:
        raise UnsupportedPool(f"pool {launcher_id.hex()[:12]} is protocol {declared}: no fee reader is pinned for it yet "
                              f"(this one reads protocols {MIN_PROTOCOL}-{MAX_PROTOCOL})")
    candidates = [declared] if declared is not None else sorted(REVISIONS)
    for protocol in candidates:
        if lp_asset_id(launcher_id, protocol) == lp_asset_id_:
            return protocol
    rev = REVISIONS[declared] if declared is not None else None
    what = f"the {rev.name} LP TAIL" if rev else "any supported revision's LP TAIL"
    which = f"a Forge {rev.name} (protocol {rev.protocol})" if rev else "a Forge V16 or V17"
    raise UnsupportedPool(f"pool {launcher_id.hex()[:12]}: LP asset {lp_asset_id_.hex()[:12]} is not {what} "
                          f"for this launcher, so it is not {which} pool")


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
                     clvm: Optional[Clvm] = None, protocol: Optional[int] = None) -> Tuple[List[str], List[Swap]]:
    """The leaves one spend of a V16 or V17 pool coin ran, in execution order, and a `Swap` for
    each swap among them. `protocol`, when known, must be the revision the leaves belong to."""
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
        raise UnsupportedPool(f"pool {launcher_id.hex()[:12]} is not on the V16/V17 action layer")
    _finalizer, _root, state = layer.args
    reserves = _ints(state.first())

    inner_solution = clvm.deserialize(solution).to_list()[2]
    puzzles, selectors, solutions = inner_solution.to_list()[:3]
    selector_list = [entry.first() for entry in selectors.to_list()][::-1]
    solution_list = solutions.to_list()
    if len(selector_list) != len(solution_list):
        raise FeeReadError("a pool spend has a selector count its solutions do not match")

    def leaf_for(selector):
        # The action layer runs `selector(...puzzles)`: a path atom is that path, and any other
        # program (`(q . leaf)`) is just as valid on chain (CE-13 / FP-07). Run it the same way.
        try:
            return selector.run(puzzles, 11_000_000_000, False).value
        except Exception as exc:  # noqa: BLE001 -- the action layer could not have run it either
            raise FeeReadError("a pool spend names a leaf selector that does not run on its revealed leaves") from exc

    names, swaps = [], []
    # The protocols every leaf so far belongs to; the spend must fit one of them (and `protocol`).
    candidates = frozenset(REVISIONS) if protocol is None else frozenset({protocol})
    for selector, leaf_solution in zip(selector_list, solution_list):
        leaf = leaf_for(selector).uncurry()
        found = LEAVES_BY_HASH.get(leaf.program.tree_hash()) if leaf is not None else None
        if found is None:
            raise UnsupportedPool(f"pool {launcher_id.hex()[:12]} ran a leaf of no supported revision")
        leaf_protocols, name = found
        if not candidates & leaf_protocols:
            raise UnsupportedPool(f"pool {launcher_id.hex()[:12]} ran a protocol {'/'.join(map(str, sorted(leaf_protocols)))} "
                                  f"leaf, not protocol {'/'.join(map(str, sorted(candidates)))}")
        candidates &= leaf_protocols
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
    protocol: Optional[int] = None  # V16 (17) or V17 (18); None reads whichever the leaves are
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
            _, swaps = parse_pool_spend(self.launcher_id, reveal, _hex(out["solution"]), height, clvm, self.protocol)
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
                "forge_puzzles": FORGE_PUZZLES_COMMIT, "pins": PINS_ID, "protocol": self.protocol}

    @classmethod
    def from_json(cls, data: dict) -> "PoolFeeWalk":
        protocol = data.get("protocol")
        if data.get("forge_puzzles") != FORGE_PUZZLES_COMMIT or data.get("pins", PINS_ID) != PINS_ID:
            # Read under other pins: start over rather than mix two readers' weights. A walk
            # cached before V17 (no "pins") was read under the same V16 pins, so it stands.
            return cls(bytes.fromhex(data["launcher_id"]), protocol)
        return cls(bytes.fromhex(data["launcher_id"]), protocol, bytes.fromhex(data["tip"]) if data.get("tip") else None,
                   data.get("tip_created", 0), [Swap.from_json(x) for x in data.get("swaps", [])], data.get("spends", 0))
