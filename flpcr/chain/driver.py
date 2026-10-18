"""Build, run and push distributor spends.

`Distributor` holds everything needed to spend one distributor: its puzzles,
the current singleton and reserve coins, the current state (as the CLVM value
the puzzle holds) and the claim-slot list. `spend(actions, extra)` runs each
leaf locally to thread the state, assembles the action-layer solution, the
reserve's delegated puzzle and every slot spend the leaves asked for, and
returns the coin spends; `push` sends them to a node (here, the simulator).

Conventions the upstream puzzles fix, which this file must follow exactly:
  * `puzzles` lists each distinct leaf once; a selector is the CLVM path to
    its leaf in that list (2, 5, 11, ...).
  * `selectors_and_proofs` is in reverse execution order; `solutions` is in
    execution order, one per action.
  * The reserve's delegated puzzle is (q . (recreate . tagged)), where
    `tagged` is each action's -42 conditions reversed, concatenated in
    execution order (the reserve finalizer prepends as it walks).
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from chia_wallet_sdk import (
    Cat,
    CatInfo,
    CatSpend,
    Clvm,
    Coin,
    CoinSpend,
    Constants,
    Program,
    Spend,
    cat_puzzle_hash,
    curry_tree_hash,
    singleton_struct_hash,
)

from ..clvm import sha256tree
from ..merkle import build_tree
from .values import to_bytes, to_int, to_list, to_program

COMPILED = pathlib.Path(__file__).resolve().parents[2] / "contracts" / "compiled"
LEAVES = ["init", "create", "post_root", "veto", "claim", "reclaim", "collect", "rotate"]
SLOT_NONCE = 1
RESERVE_NONCE = 0
RESERVE_TAG = -42
MODE_PUZZLE_TO_COIN = 23
LOW = bytes(32)
HIGH = b"\xff" * 32
MAX_COST = 11_000_000_000


class Puzzles:
    """Compiled puzzle mods, loaded once."""

    def __init__(self, clvm: Clvm):
        self.clvm = clvm
        self.mods: Dict[str, Program] = {}
        for path in COMPILED.glob("*.hex"):
            self.mods[path.stem] = clvm.deserialize(bytes.fromhex(path.read_text().strip()))
        missing = {"action", "reserve_finalizer", "slot", "p2_delegated_by_singleton", *LEAVES} - set(self.mods)
        if missing:
            raise FileNotFoundError(f"compiled puzzles missing {sorted(missing)}; run contracts/build.py")
        self.singleton_mod = clvm.singleton_top_layer_v1_1()
        self.launcher = clvm.singleton_launcher()

    def hash(self, name: str) -> bytes:
        return self.mods[name].tree_hash()


@dataclass(frozen=True)
class DistributorConfig:
    reward_asset: bytes  # bytes(32) for XCH, else the CAT asset id
    fee_bps: int
    treasury_ph: bytes
    guardian_ph: bytes
    router_ph: bytes
    engine_ph: bytes
    dispute_blocks: int
    min_campaign_length: int
    max_campaign_length: int
    max_campaigns: int = 32


@dataclass
class SlotRecord:
    claimed: int
    left: bytes
    right: bytes
    # The singleton coin that created the slot: (its parent id, its inner puzzle hash).
    creator_parent: bytes
    creator_inner_ph: bytes

    def value(self, key: bytes):
        return (key, (self.claimed, (self.left, self.right)))


@dataclass
class Action:
    leaf: str
    solution: list  # Python values; `h` first


class Distributor:
    def __init__(self, puzzles: Puzzles, cfg: DistributorConfig, launcher_id: bytes):
        self.p = puzzles
        self.clvm = puzzles.clvm
        self.cfg = cfg
        self.launcher_id = launcher_id
        c = self.clvm
        self.struct_hash = singleton_struct_hash(launcher_id)
        self.singleton_struct = to_program(
            c, (Constants.singleton_top_layer_v1_1_hash(), (launcher_id, Constants.singleton_launcher_hash()))
        )

        self.reserve_puzzle = puzzles.mods["p2_delegated_by_singleton"].curry(
            [c.atom(Constants.singleton_top_layer_v1_1_hash()), c.atom(self.struct_hash), to_program(c, RESERVE_NONCE)]
        )
        self.reserve_ph = self.reserve_puzzle.tree_hash()
        self.is_cat = cfg.reward_asset != bytes(32)
        self.reserve_full_ph = (
            cat_puzzle_hash(cfg.reward_asset, self.reserve_ph) if self.is_cat else self.reserve_ph
        )

        self.slot_1st = puzzles.mods["slot"].curry(
            [to_program(c, (Constants.singleton_top_layer_v1_1_hash(), self.struct_hash)), to_program(c, SLOT_NONCE)]
        )
        self.slot_1st_hash = self.slot_1st.tree_hash()

        self.config_value = [
            launcher_id,
            cfg.reward_asset,
            cfg.fee_bps,
            cfg.treasury_ph,
            cfg.guardian_ph,
            cfg.router_ph,
            cfg.dispute_blocks,
            cfg.min_campaign_length,
            cfg.max_campaign_length,
            cfg.max_campaigns,
            self.slot_1st_hash,
        ]
        config_program = to_program(c, self.config_value)
        self.leaves = {name: puzzles.mods[name].curry([config_program]) for name in LEAVES}
        self.leaf_hashes = {name: prog.tree_hash() for name, prog in self.leaves.items()}
        self.merkle_root, self.leaf_proofs = build_tree([self.leaf_hashes[n] for n in LEAVES])

        finalizer_1st = puzzles.mods["reserve_finalizer"].curry(
            [
                c.atom(puzzles.hash("action")),
                c.atom(self.reserve_full_ph),
                c.atom(self.reserve_ph),
                to_program(c, 2),  # amount = (f State)
                c.atom(launcher_id),  # hint
            ]
        )
        self.finalizer = finalizer_1st.curry([c.atom(finalizer_1st.tree_hash())])

        # Live chain state, kept by `apply`.
        self.state: Program = to_program(
            c, [0, 0, cfg.engine_ph, 0, None, None, None]
        )
        self.coin: Optional[Coin] = None
        self.lineage: Optional[list] = None
        self.reserve_coin: Optional[Coin] = None  # the full coin, XCH or CAT
        self.reserve_cat: Optional[Cat] = None  # set for a CAT reserve
        self.slots: Dict[bytes, SlotRecord] = {}

    # --- puzzles -------------------------------------------------------------------

    def inner_puzzle(self, state: Program) -> Program:
        return self.p.mods["action"].curry([self.finalizer, self.clvm.atom(self.merkle_root), state])

    def full_puzzle(self, state: Program) -> Program:
        return self.p.singleton_mod.curry([self.singleton_struct, self.inner_puzzle(state)])

    def slot_puzzle(self, value) -> Program:
        return self.slot_1st.curry([self.clvm.atom(sha256tree(value))])

    def slot_ph(self, value) -> bytes:
        return self.slot_puzzle(value).tree_hash()

    # --- launch ----------------------------------------------------------------------

    def launch_spends(self, funding: Coin, funding_puzzle: Program,
                      seed_cat: Optional[Cat] = None, seed_puzzle: Optional[Program] = None) -> List[CoinSpend]:
        """Funding coin (anyone-can-spend `(r 1)` style) creates the launcher; the
        launcher creates the eve singleton. The empty reserve comes from the
        funding coin for XCH, or for a CAT from `seed_cat`, a coin of the reward
        CAT whose inner puzzle is `seed_puzzle` (its value goes back to its
        owner; only a 0-mojo reserve child is added)."""
        c = self.clvm
        launcher_coin = Coin(funding.coin_id(), Constants.singleton_launcher_hash(), 1)
        assert launcher_coin.coin_id() == self.launcher_id
        eve_ph = self.full_puzzle(self.state).tree_hash()
        funding_conditions = [[51, Constants.singleton_launcher_hash(), 1]]
        if not self.is_cat:
            funding_conditions.append([51, self.reserve_ph, 0])
        launcher_solution = to_program(c, [eve_ph, 1, None])
        self.coin = Coin(self.launcher_id, eve_ph, 1)
        self.lineage = [funding.coin_id(), 1]
        spends = [
            CoinSpend(funding, funding_puzzle.serialize(), to_program(c, funding_conditions).serialize()),
            CoinSpend(launcher_coin, self.p.launcher.serialize(), launcher_solution.serialize()),
        ]
        if not self.is_cat:
            self.reserve_coin = Coin(funding.coin_id(), self.reserve_ph, 0)
            return spends
        if seed_cat is None or seed_puzzle is None:
            raise ValueError("a CAT distributor needs a seed coin of the reward CAT")
        owner = seed_cat.info.p2_puzzle_hash
        seed_conditions = [[51, self.reserve_ph, 0, [self.reserve_ph]], [51, owner, seed_cat.coin.amount, [owner]]]
        c.spend_cats([CatSpend(seed_cat, Spend(seed_puzzle, to_program(c, seed_conditions)))])
        spends.extend(c.coin_spends())
        self.reserve_cat = seed_cat.child(self.reserve_ph, 0)
        self.reserve_coin = self.reserve_cat.coin
        return spends

    # --- running leaves locally ---------------------------------------------------------

    def run_leaf(self, name: str, truth: Program, solution: list) -> Tuple[Program, List[Program]]:
        out = self.leaves[name].run(self.clvm.pair(truth, to_program(self.clvm, solution)), MAX_COST, False)
        return out.value.first(), to_list(out.value.rest())

    def simulate(self, actions: Sequence[Action]) -> Tuple[Program, List[List[Program]]]:
        truth = self.clvm.pair(self.clvm.nil(), self.state)
        per_action = []
        for a in actions:
            truth, conditions = self.run_leaf(a.leaf, truth, a.solution)
            per_action.append(conditions)
        return truth.rest(), per_action

    # --- building a spend ------------------------------------------------------------------

    def spend(self, actions: Sequence[Action], cat_spends: Sequence[CatSpend] = ()
              ) -> Tuple[List[CoinSpend], Program, List[List[Program]]]:
        """`cat_spends`: other coins of the reward CAT that must sit in the
        reserve's CAT ring, i.e. a creator's deposit."""
        if not actions:
            raise ValueError("a spend runs at least one action")
        c = self.clvm
        new_state, per_action = self.simulate(actions)

        # Action layer solution.
        distinct: List[str] = []
        for a in actions:
            if a.leaf not in distinct:
                distinct.append(a.leaf)
        selector = {name: (1 << (i + 1)) + (1 << i) - 1 for i, name in enumerate(distinct)}
        selectors_and_proofs = []
        for a in reversed(actions):
            path, hashes = self.leaf_proofs[self.leaf_hashes[a.leaf]]
            selectors_and_proofs.append((selector[a.leaf], (path, hashes)))
        inner_solution = to_program(
            c,
            (
                [self.leaves[n] for n in distinct],
                (selectors_and_proofs, ([a.solution for a in actions], self.reserve_coin.parent_coin_info)),
            ),
        )
        inner_ph = self.inner_puzzle(self.state).tree_hash()
        singleton_solution = to_program(c, [self.lineage, 1, inner_solution])
        spends = [
            CoinSpend(self.coin, self.full_puzzle(self.state).serialize(), singleton_solution.serialize())
        ]

        # Reserve: recreate at the new amount and run every tagged condition.
        new_reserve = to_int(new_state.first())
        tagged: List[Program] = []
        for conditions in per_action:
            mine = [cond.rest() for cond in conditions if cond.is_pair() and cond.first().is_atom() and to_int(cond.first()) == RESERVE_TAG]
            tagged.extend(reversed(mine))
        recreate = to_program(c, [51, self.reserve_ph, new_reserve, [self.reserve_ph]])
        delegated = c.pair(c.atom(b"\x01"), c.list([recreate, *tagged]))
        reserve_solution = to_program(c, [inner_ph, delegated])
        if self.is_cat:
            c.spend_cats([CatSpend(self.reserve_cat, Spend(self.reserve_puzzle, reserve_solution)), *cat_spends])
            spends.extend(c.coin_spends())
        else:
            if cat_spends:
                raise ValueError("an XCH distributor takes no CAT spends")
            spends.append(CoinSpend(self.reserve_coin, self.reserve_puzzle.serialize(), reserve_solution.serialize()))

        # Slots the leaves spend: every SEND_MESSAGE mode 18 to a slot puzzle hash.
        by_ph = {self.slot_ph(rec.value(key)): (key, rec) for key, rec in self.slots.items()}
        for conditions in per_action:
            for cond in conditions:
                items = to_list(cond) if cond.is_pair() else []
                if len(items) >= 4 and items[0].is_atom() and to_int(items[0]) == 66 and to_int(items[1]) == 18:
                    target = to_bytes(items[3])
                    if target not in by_ph:
                        raise KeyError("a leaf spends a slot the driver does not know")
                    key, rec = by_ph[target]
                    slot_coin = Coin(
                        Coin(rec.creator_parent, self.full_puzzle_hash_for_inner(rec.creator_inner_ph), 1).coin_id(),
                        target,
                        0,
                    )
                    slot_solution = to_program(c, ((rec.creator_parent, (rec.creator_inner_ph, 1)), inner_ph))
                    spends.append(CoinSpend(slot_coin, self.slot_puzzle(rec.value(key)).serialize(), slot_solution.serialize()))
        return spends, new_state, per_action

    def full_puzzle_hash_for_inner(self, inner_ph: bytes) -> bytes:
        return curry_tree_hash(Constants.singleton_top_layer_v1_1_hash(), [self.singleton_struct.tree_hash(), inner_ph])

    def apply(self, new_state: Program, slot_updates: Dict[bytes, Optional[Tuple[int, bytes, bytes]]]) -> None:
        """Advance to the generation `spend` produced, once it has confirmed."""
        old_coin = self.coin
        old_inner_ph = self.inner_puzzle(self.state).tree_hash()
        for key, value in slot_updates.items():
            if value is None:
                self.slots.pop(key, None)
            else:
                claimed, left, right = value
                self.slots[key] = SlotRecord(claimed, left, right, old_coin.parent_coin_info, old_inner_ph)
        self.lineage = [old_coin.parent_coin_info, old_inner_ph, 1]
        self.state = new_state
        self.coin = Coin(old_coin.coin_id(), self.full_puzzle(new_state).tree_hash(), 1)
        new_reserve = to_int(new_state.first())
        if self.is_cat:
            self.reserve_cat = self.reserve_cat.child(self.reserve_ph, new_reserve)
            self.reserve_coin = self.reserve_cat.coin
        else:
            self.reserve_coin = Coin(self.reserve_coin.coin_id(), self.reserve_ph, new_reserve)

    # --- reading state --------------------------------------------------------------------

    def read_state(self) -> dict:
        f = to_list(self.state)

        def root(p):
            if p.is_null():
                return None
            r = to_list(p)
            return {
                "root_id": to_int(r[0]),
                "start": to_int(r[1]),
                "end": to_int(r[2]),
                "root": to_bytes(r[3]),
                "totals": {to_bytes(t.first()): to_int(t.rest()) for t in to_list(r[4])},
                "activates_at": to_int(r[5]),
            }

        campaigns = {}
        for camp in to_list(f[4]):
            v = to_list(camp)
            campaigns[to_bytes(v[0])] = {
                "creator": to_bytes(v[1]),
                "budget": to_int(v[2]),
                "start": to_int(v[3]),
                "end": to_int(v[4]),
                "claim_window": to_int(v[5]),
                "claimed": to_int(v[6]),
            }
        return {
            "reserve": to_int(f[0]),
            "fees_owed": to_int(f[1]),
            "engine_ph": to_bytes(f[2]),
            "initialized": to_int(f[3]),
            "campaigns": campaigns,
            "active": root(f[5]),
            "pending": root(f[6]),
        }

    # --- slot list helpers ------------------------------------------------------------------

    @staticmethod
    def claim_key(campaign_id: bytes, puzzle_hash: bytes) -> bytes:
        return sha256tree((campaign_id, puzzle_hash))

    def neighbours(self, key: bytes) -> Tuple[bytes, bytes]:
        left = max(k for k in self.slots if k < key)
        right = min(k for k in self.slots if k > key)
        return left, right

    def claim_slot_info(self, key: bytes, cumulative: int):
        """(insert flag, slot_info, slot updates to apply once confirmed)."""
        if key in self.slots:
            rec = self.slots[key]
            return 0, (rec.claimed, (rec.left, rec.right)), {key: (cumulative, rec.left, rec.right)}
        lk, rk = self.neighbours(key)
        l, r = self.slots[lk], self.slots[rk]
        info = [lk, l.claimed, l.left, rk, r.claimed, r.right]
        updates = {
            lk: (l.claimed, l.left, key),
            rk: (r.claimed, key, r.right),
            key: (cumulative, lk, rk),
        }
        return 1, info, updates


# --- simulator helpers ---------------------------------------------------------------------


class Keys:
    """Anyone-can-spend `(r 1)` puzzles curried with a label: distinct puzzle
    hashes standing in for the router, operator, guardian, holders and creators."""

    def __init__(self, clvm: Clvm):
        self.clvm = clvm
        self.base = clvm.parse("(r 1)")

    def puzzle(self, label: str) -> Program:
        return self.base.curry([self.clvm.atom(label.encode())])

    def ph(self, label: str) -> bytes:
        return self.puzzle(label).tree_hash()

    def spend(self, coin: Coin, label: str, conditions: list) -> CoinSpend:
        return CoinSpend(coin, self.puzzle(label).serialize(), to_program(self.clvm, conditions).serialize())


def send_message_condition(message: bytes, receiver_coin_id: bytes) -> list:
    return [66, MODE_PUZZLE_TO_COIN, message, receiver_coin_id]
