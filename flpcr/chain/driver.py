"""Build distributor spends, and track the distributor between them.

`Distributor` holds everything needed to spend one distributor: its puzzles,
the current singleton and reserve coins, the current state and both slot lists
(claims and delegations). It never signs, spends a wallet coin or talks to a
node: that is the caller's job (the simulator in tests, a wallet or deploy
script on testnet).

The flow is three steps:
  1. a per-leaf builder (`create`, `post_root`, `claim`, ...) returns a
     `LeafCall`: the action's solution, the messages, signatures and network
     fee the bundle must carry, and the slot records the action changes;
  2. `prepare(calls)` runs every action locally to thread the state and
     returns a `Prepared`: the coin spends, the new state, the slot updates
     and everything the caller must add to the bundle. It refuses a batch
     whose actions would collide in one spend (review 2026-10-19, FLPCR-13);
  3. once the bundle has confirmed, `apply(prepared)` advances everything at
     once.

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

import os
import pathlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from chia_wallet_sdk import (
    Cat,
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
from ..distributor import HEIGHT_WINDOW, Config
from ..leaves import LEAVES
from ..merkle import build_tree, claim_leaf
from .auth import (
    AUTH_ADMIN,
    AUTH_DELEGATE,
    AUTH_MESSAGE,
    AUTH_SIGNATURE,
    Auth,
    ByAdmin,
    ByDelegate,
    ByMessage,
    BySignature,
    Message,
    Signature,
)
from .state import ChainState
from .values import to_bytes, to_int, to_list, to_program

COMPILED = pathlib.Path(
    os.environ.get("FLPCR_COMPILED") or pathlib.Path(__file__).resolve().parents[2] / "contracts" / "compiled"
)
SLOT_NONCE = 1
DELEGATION_SLOT_NONCE = 2
RESERVE_NONCE = 0
BOND_NONCE = 3  # p2_delegated_by_singleton holding a dispute bond
RESERVE_TAG = -42
from .auth import MODE_PUZZLE_TO_ANY, MODE_PUZZLE_TO_COIN  # noqa: E402
MODE_PUZZLE_TO_PUZZLE = 18  # a slot spend: the singleton's message to the slot's puzzle hash
OP_CREATE_COIN = 51
OP_SEND_MESSAGE = 66
LOW = bytes(32)
HIGH = b"\xff" * 32
MAX_COST = 11_000_000_000


class BundleCollision(ValueError):
    """Actions that cannot share one singleton spend: two equal reserve
    outputs (one coin id twice, DUPLICATE_OUTPUT), or two actions touching the
    same slot (DOUBLE_SPEND or a repeated recreation). Split them across
    spends."""


class Puzzles:
    """Compiled puzzle mods, loaded once."""

    def __init__(self, clvm: Clvm):
        self.clvm = clvm
        self.mods: Dict[str, Program] = {}
        for path in COMPILED.glob("*.hex"):
            self.mods[path.stem] = clvm.deserialize(bytes.fromhex(path.read_text().strip()))
        missing = {"action", "reserve_finalizer", "slot", "p2_delegated_by_singleton", "bond_launcher", *LEAVES} - set(self.mods)
        if missing:
            raise FileNotFoundError(f"compiled puzzles missing {sorted(missing)}; run contracts/build.py")
        self.singleton_mod = clvm.singleton_top_layer_v1_1()
        self.launcher = clvm.singleton_launcher()

    def hash(self, name: str) -> bytes:
        return self.mods[name].tree_hash()


# --- the two slot lists ---------------------------------------------------------------


@dataclass
class ClaimRecord:
    """A claim slot (shared.rue ClaimSlot) and the singleton generation that
    created it: (its parent id, its inner puzzle hash)."""

    claimed: int
    left: bytes
    right: bytes
    creator_parent: bytes
    creator_inner_ph: bytes

    def value(self, key: bytes):
        return (key, (self.claimed, (self.left, self.right)))


@dataclass
class DelegationRecord:
    """A delegation slot (shared.rue DelegationSlot) and its creator."""

    active: int
    uses: int
    left: bytes
    right: bytes
    creator_parent: bytes
    creator_inner_ph: bytes

    def value(self, key: bytes):
        return (key, (self.active, (self.uses, (self.left, self.right))))


ClaimUpdate = Optional[Tuple[int, bytes, bytes]]  # (claimed, left, right); None removes
DelegationUpdate = Tuple[int, int, bytes, bytes]  # (active, uses, left, right)


def neighbours(records: Dict[bytes, object], key: bytes) -> Tuple[bytes, bytes]:
    left = max(k for k in records if k < key)
    right = min(k for k in records if k > key)
    return left, right


# --- calls and prepared bundles ---------------------------------------------------------


@dataclass
class Action:
    leaf: str
    solution: list  # Python values; `h` first


@dataclass
class LeafCall:
    """One action, and what the bundle around it must supply."""

    action: Action
    messages: List[Message] = field(default_factory=list)
    signatures: List[Signature] = field(default_factory=list)
    fee: int = 0  # network fee the leaf reserves (RESERVE_FEE)
    claim_updates: Dict[bytes, ClaimUpdate] = field(default_factory=dict)
    delegation_updates: Dict[bytes, DelegationUpdate] = field(default_factory=dict)
    # Coins the caller's own spend must create: (puzzle hash, amount, memo).
    # A dispute's bond launcher is one.
    creations: List[Tuple[bytes, int, bytes]] = field(default_factory=list)
    # Spends the driver builds beside the singleton's: the bond launcher, the bond coin.
    extra_spends: List[CoinSpend] = field(default_factory=list)

    @property
    def touched(self) -> Set[Tuple[str, bytes]]:
        return {("claim", k) for k in self.claim_updates} | {("delegation", k) for k in self.delegation_updates}


@dataclass
class Prepared:
    spends: List[CoinSpend]
    new_state: Program
    per_action: List[List[Program]]
    messages: List[Message]
    signatures: List[Signature]
    fee: int
    claim_updates: Dict[bytes, ClaimUpdate]
    delegation_updates: Dict[bytes, DelegationUpdate]
    # (puzzle hash, amount, memo) the caller's own spend must create.
    creations: List[Tuple[bytes, int, bytes]] = field(default_factory=list)
    # Slot puzzle hashes a leaf messaged that no known record matches. The
    # bundle is still built: consensus then refuses it for want of a
    # receiver, which is the refusal a probe observes. A real builder should
    # treat a non-empty list as "resync first".
    unresolved: List[bytes] = field(default_factory=list)

    @property
    def state(self) -> ChainState:
        return ChainState.from_program(self.new_state)


@dataclass(frozen=True)
class ClaimRequest:
    """One holder's claim as a sequencer receives it: the root's proof, and
    the holder's authorization (a key signature or the holder's own coin
    spend), neither of which names a generation."""

    campaign_id: bytes
    puzzle_hash: bytes
    cumulative: int
    proof: tuple
    auth: Auth = ByMessage()


@dataclass
class ClaimBatch:
    """Claims packed into chained generations of the singleton, all in one
    bundle: generation N+1 spends the singleton coin, the reserve and the slots
    generation N created. `generations` apply in order once the bundle has
    confirmed (`Distributor.apply_batch`)."""

    generations: List[Prepared]
    placed: List[ClaimRequest]
    deferred: List[ClaimRequest]  # left for the next bundle (cost budget)
    rejected: List[Tuple[ClaimRequest, str]]  # the leaf refused them (proof, amount, auth)
    cost: int

    @property
    def spends(self) -> List[CoinSpend]:
        return [s for g in self.generations for s in g.spends]

    @property
    def messages(self) -> List[Message]:
        return [m for g in self.generations for m in g.messages]

    @property
    def signatures(self) -> List[Signature]:
        return [s for g in self.generations for s in g.signatures]

    @property
    def unresolved(self) -> List[bytes]:
        return [u for g in self.generations for u in g.unresolved]


class Distributor:
    def __init__(self, puzzles: Puzzles, cfg: Config, engine_ph: bytes):
        self.p = puzzles
        self.clvm = puzzles.clvm
        self.cfg = cfg
        self.launcher_id = cfg.distributor_id
        c = self.clvm
        self.struct_hash = singleton_struct_hash(self.launcher_id)
        self.singleton_struct = to_program(
            c, (Constants.singleton_top_layer_v1_1_hash(), (self.launcher_id, Constants.singleton_launcher_hash()))
        )

        self.reserve_puzzle = puzzles.mods["p2_delegated_by_singleton"].curry(
            [c.atom(Constants.singleton_top_layer_v1_1_hash()), c.atom(self.struct_hash), to_program(c, RESERVE_NONCE)]
        )
        self.reserve_ph = self.reserve_puzzle.tree_hash()
        self.is_cat = cfg.reward_asset != bytes(32)
        self.reserve_full_ph = (
            cat_puzzle_hash(cfg.reward_asset, self.reserve_ph) if self.is_cat else self.reserve_ph
        )

        struct = to_program(c, (Constants.singleton_top_layer_v1_1_hash(), self.struct_hash))
        self.slot_1st = puzzles.mods["slot"].curry([struct, to_program(c, SLOT_NONCE)])
        self.delegation_1st = puzzles.mods["slot"].curry([struct, to_program(c, DELEGATION_SLOT_NONCE)])
        # Where a dispute bond sits: the same delegated-by-singleton puzzle as
        # the reserve, under its own nonce, always bare XCH.
        self.bond_puzzle = puzzles.mods["p2_delegated_by_singleton"].curry(
            [c.atom(Constants.singleton_top_layer_v1_1_hash()), c.atom(self.struct_hash), to_program(c, BOND_NONCE)]
        )
        self.bond_ph = self.bond_puzzle.tree_hash()
        self.bond_launcher_hash = puzzles.hash("bond_launcher")

        config_program = to_program(c, self.config_value())
        self.leaves = {name: puzzles.mods[name].curry([config_program]) for name in LEAVES}
        self.leaf_hashes = {name: prog.tree_hash() for name, prog in self.leaves.items()}
        self.merkle_root, self.leaf_proofs = build_tree([self.leaf_hashes[n] for n in LEAVES])

        finalizer_1st = puzzles.mods["reserve_finalizer"].curry(
            [
                c.atom(puzzles.hash("action")),
                c.atom(self.reserve_full_ph),
                c.atom(self.reserve_ph),
                to_program(c, 2),  # amount = (f State)
                c.atom(self.launcher_id),  # hint
            ]
        )
        self.finalizer = finalizer_1st.curry([c.atom(finalizer_1st.tree_hash())])

        # Live chain state, kept by `apply`.
        self.state: Program = to_program(c, ChainState.genesis(engine_ph).to_value())
        self.coin: Optional[Coin] = None
        self.lineage: Optional[list] = None
        self.reserve_coin: Optional[Coin] = None  # the full coin, XCH or CAT
        self.reserve_cat: Optional[Cat] = None  # set for a CAT reserve
        self.claims: Dict[bytes, ClaimRecord] = {}
        self.delegations: Dict[bytes, DelegationRecord] = {}
        # campaign id -> the addresses its creator excluded (params' blacklist),
        # which every claim on that campaign reveals.
        self.excluded: Dict[bytes, List[bytes]] = {}

    def config_value(self) -> list:
        """shared.rue Config, in field order."""
        cfg = self.cfg
        return [
            self.launcher_id,
            cfg.reward_asset,
            cfg.fee_bps,
            cfg.dev_fee_ph,
            cfg.admin_ph,
            cfg.guardian_ph,
            cfg.router_ph,
            cfg.dispute_blocks,
            cfg.rotate_delay,
            cfg.delegate_fee,
            cfg.min_campaign_length,
            cfg.max_campaign_length,
            cfg.max_campaigns,
            self.slot_1st.tree_hash(),
            self.delegation_1st.tree_hash(),
            cfg.dispute_bond,
            self.bond_ph,
            self.bond_launcher_hash,
        ]

    # --- puzzles -------------------------------------------------------------------

    def inner_puzzle(self, state: Program) -> Program:
        return self.p.mods["action"].curry([self.finalizer, self.clvm.atom(self.merkle_root), state])

    def full_puzzle(self, state: Program) -> Program:
        return self.p.singleton_mod.curry([self.singleton_struct, self.inner_puzzle(state)])

    def full_puzzle_hash_for_inner(self, inner_ph: bytes) -> bytes:
        return curry_tree_hash(Constants.singleton_top_layer_v1_1_hash(), [self.singleton_struct.tree_hash(), inner_ph])

    def claim_slot_puzzle(self, key: bytes, rec: ClaimRecord) -> Program:
        return self.slot_1st.curry([self.clvm.atom(sha256tree(rec.value(key)))])

    def delegation_slot_puzzle(self, key: bytes, rec: DelegationRecord) -> Program:
        return self.delegation_1st.curry([self.clvm.atom(sha256tree(rec.value(key)))])

    @property
    def chain_state(self) -> ChainState:
        return ChainState.from_program(self.state)

    # --- launch ----------------------------------------------------------------------

    def funding_conditions(self, funding: Coin) -> list:
        """What the coin that launches this distributor must create: the
        launcher, and for XCH the empty reserve. The caller spends `funding`
        with these conditions (plus its own change and fee) however its puzzle
        is spent: an anyone-can-spend test puzzle in the simulator, a standard
        wallet coin signed by Sage on testnet."""
        if Coin(funding.coin_id(), Constants.singleton_launcher_hash(), 1).coin_id() != self.launcher_id:
            raise ValueError("this distributor's launcher id is not derived from that funding coin")
        conditions = [[OP_CREATE_COIN, Constants.singleton_launcher_hash(), 1]]
        if not self.is_cat:
            conditions.append([OP_CREATE_COIN, self.reserve_ph, 0])
        return conditions

    def launcher_spend(self, funding: Coin) -> CoinSpend:
        """The launcher's own spend, creating the eve singleton; records the eve
        coin, its lineage and (for XCH) the empty reserve the funding coin made."""
        c = self.clvm
        launcher_coin = Coin(funding.coin_id(), Constants.singleton_launcher_hash(), 1)
        eve_ph = self.full_puzzle(self.state).tree_hash()
        self.coin = Coin(self.launcher_id, eve_ph, 1)
        self.lineage = [funding.coin_id(), 1]
        if not self.is_cat:
            self.reserve_coin = Coin(funding.coin_id(), self.reserve_ph, 0)
        return CoinSpend(launcher_coin, self.p.launcher.serialize(), to_program(c, [eve_ph, 1, None]).serialize())

    def launch_spends(self, funding: Coin, funding_puzzle: Program,
                      seed_cat: Optional[Cat] = None, seed_puzzle: Optional[Program] = None) -> List[CoinSpend]:
        """Funding coin (anyone-can-spend `(r 1)` style) creates the launcher; the
        launcher creates the eve singleton. The empty reserve comes from the
        funding coin for XCH, or for a CAT from `seed_cat`, a coin of the reward
        CAT whose inner puzzle is `seed_puzzle` (its value goes back to its
        owner; only a 0-mojo reserve child is added)."""
        c = self.clvm
        conditions = self.funding_conditions(funding)
        spends = [
            CoinSpend(funding, funding_puzzle.serialize(), to_program(c, conditions).serialize()),
            self.launcher_spend(funding),
        ]
        if not self.is_cat:
            return spends
        if seed_cat is None or seed_puzzle is None:
            raise ValueError("a CAT distributor needs a seed coin of the reward CAT")
        owner = seed_cat.info.p2_puzzle_hash
        seed_conditions = [[OP_CREATE_COIN, self.reserve_ph, 0, [self.reserve_ph]],
                           [OP_CREATE_COIN, owner, seed_cat.coin.amount, [owner]]]
        c.spend_cats([CatSpend(seed_cat, Spend(seed_puzzle, to_program(c, seed_conditions)))])
        spends.extend(c.coin_spends())
        self.reserve_cat = seed_cat.child(self.reserve_ph, 0)
        self.reserve_coin = self.reserve_cat.coin
        return spends

    # --- authorization -----------------------------------------------------------------

    def _authorize(self, owner: bytes, auth: Auth, message: bytes, claim: bool = False) -> Tuple[list, LeafCall]:
        """(the leaf's `auth, auth_data` fields, a LeafCall carrying what the
        bundle must supply and any delegation slot update). `message` is the
        32-byte message the leaf authorizes. A `claim` names no generation:
        its message is mode 16 and its signature AGG_SIG_UNSAFE."""
        req = LeafCall(Action("", []))
        mode = MODE_PUZZLE_TO_ANY if claim else MODE_PUZZLE_TO_COIN
        if isinstance(auth, BySignature):
            req.signatures.append(Signature(auth.public_key, message, unsafe=claim))
            return [AUTH_SIGNATURE, auth.public_key], req
        if isinstance(auth, ByMessage):
            req.messages.append(Message(owner, b"k" + message, mode))
            return [AUTH_MESSAGE, None], req
        if isinstance(auth, ByAdmin):
            req.messages.append(Message(self.cfg.admin_ph, b"k" + message))
            return [AUTH_ADMIN, None], req
        if isinstance(auth, ByDelegate):
            key = self.delegation_key(owner, auth.delegate)
            rec = self.delegations.get(key)
            # An ungranted pair still builds (for refusal probes): its slot
            # message has no receiver and consensus refuses the bundle.
            uses, left, right = (rec.uses, rec.left, rec.right) if rec else (0, LOW, HIGH)
            if isinstance(auth.via, BySignature):
                inner = (AUTH_SIGNATURE, auth.via.public_key)
                req.signatures.append(Signature(auth.via.public_key, message, unsafe=claim))
            else:
                inner = (AUTH_MESSAGE, None)
                req.messages.append(Message(auth.delegate, b"k" + message, mode))
            req.delegation_updates[key] = (1, uses + 1, left, right)
            return [AUTH_DELEGATE, (auth.delegate, (uses, (left, (right, inner))))], req
        raise TypeError(f"not an Auth: {auth!r}")

    @staticmethod
    def _with(call: LeafCall, req: LeafCall) -> LeafCall:
        call.messages.extend(req.messages)
        call.signatures.extend(req.signatures)
        call.delegation_updates.update(req.delegation_updates)
        return call

    def engine_at(self, h: int) -> bytes:
        """The operator key a spend at `h` answers to, after the prologue
        adopts any rotation whose delay has passed."""
        st = self.chain_state
        if st.rotation is not None and h >= st.rotation.effective_at:
            return st.rotation.engine_ph
        return st.engine_ph

    # --- per-leaf builders -------------------------------------------------------------

    def init(self, h: int) -> LeafCall:
        return LeafCall(
            Action("init", [h]),
            claim_updates={LOW: (0, LOW, HIGH), HIGH: (0, LOW, HIGH)},
            delegation_updates={LOW: (0, 0, LOW, HIGH), HIGH: (0, 0, LOW, HIGH)},
        )

    def campaign_id(self, creator: bytes, amount: int, start: int, end: int, kind: int, target: bytes,
                    params: list, nonce, claim_window: int) -> bytes:
        return sha256tree([
            "flpcr-campaign-v1", self.launcher_id, creator, self.cfg.reward_asset, amount, start, end, kind,
            target, sha256tree(params), nonce, self.cfg.fee_bps, claim_window,
        ])

    def create(self, h: int, creator: bytes, amount: int, start: int, end: int, kind: int, target: bytes,
               params: list, nonce, claim_window: int, auth: Auth = ByMessage(),
               router: Optional[bytes] = None) -> LeafCall:
        """The router (a coin at `router`, default router_ph) and the creator
        (by spending a coin at `creator`) both send the campaign id."""
        cid = self.campaign_id(creator, amount, start, end, kind, target, params, nonce, claim_window)
        listed = params[3] if isinstance(params, list) and len(params) > 3 else None
        self.excluded[cid] = list(listed) if isinstance(listed, (list, tuple)) else []
        fields, req = self._authorize(creator, auth, cid)
        sol = [h, creator, amount, start, end, kind, target, params, nonce, claim_window, fields[0]]
        call = LeafCall(Action("create", sol), [Message(router or self.cfg.router_ph, b"c" + cid)])
        return self._with(call, req)

    def post_root(self, h: int, root_id: int, start: int, end: int, root: bytes,
                  totals: Sequence[Tuple[bytes, int]], manifest_hash: bytes = bytes(32),
                  sender: Optional[bytes] = None) -> LeafCall:
        args = [root_id, start, end, root, list(totals), manifest_hash]
        return LeafCall(Action("post_root", [h, *args]),
                        [Message(sender or self.engine_at(h), b"p" + sha256tree(args))])

    def veto(self, h: int, root_id: int, sender: Optional[bytes] = None) -> LeafCall:
        return LeafCall(Action("veto", [h, root_id]),
                        [Message(sender or self.cfg.guardian_ph, b"v" + sha256tree(root_id))])

    def rotate(self, h: int, new_engine_ph: bytes, sender: Optional[bytes] = None) -> LeafCall:
        return LeafCall(Action("rotate", [h, new_engine_ph, 0]),
                        [Message(sender or self.cfg.guardian_ph, b"r" + new_engine_ph)])

    def cancel_rotation(self, h: int, engine_ph: bytes, sender: Optional[bytes] = None) -> LeafCall:
        return LeafCall(Action("rotate", [h, engine_ph, 1]),
                        [Message(sender or self.cfg.admin_ph, b"x" + engine_ph)])

    # --- disputes ---------------------------------------------------------------------

    @staticmethod
    def bond_message(bond_ph: bytes, amount: int, distributor_id: bytes) -> bytes:
        return b"flpcr-bond-v1" + sha256tree([bond_ph, amount, distributor_id])

    def bond_launcher_spend(self, launcher_coin: Coin) -> CoinSpend:
        """The bond launcher creating the bond coin for its whole amount."""
        solution = to_program(self.clvm, [self.bond_ph, launcher_coin.amount, self.launcher_id])
        return CoinSpend(launcher_coin, self.p.mods["bond_launcher"].serialize(), solution.serialize())

    def dispute(self, h: int, root_id: int, disputer_ph: bytes, disputer_coin_id: bytes) -> LeafCall:
        """Contest the pending root. The caller's spend of `disputer_coin_id`
        must create the bond launcher (see `creations`); the driver spends the
        launcher, which creates the bond coin the leaf asserts."""
        bond = self.cfg.dispute_bond
        launcher = Coin(disputer_coin_id, self.bond_launcher_hash, bond)
        return LeafCall(
            Action("dispute", [h, root_id, disputer_ph, disputer_coin_id]),
            creations=[(self.bond_launcher_hash, bond, self.launcher_id)],
            extra_spends=[self.bond_launcher_spend(launcher)],
        )

    def resolve(self, h: int, upheld: int, sender: Optional[bytes] = None) -> LeafCall:
        """The guardian (default) or admin settles the open dispute; the bond
        coin is spent in the same bundle, paying the disputer or the dev multisig."""
        st = self.chain_state
        if st.dispute is None:
            raise ValueError("no dispute open")
        sender = sender or self.cfg.guardian_ph
        bond = self.cfg.dispute_bond
        bond_coin = Coin(st.dispute.bond_parent, self.bond_ph, bond)
        recipient = st.dispute.disputer_ph if upheld else self.cfg.dev_fee_ph
        delegated = to_program(self.clvm, (1, [[OP_CREATE_COIN, recipient, bond, [recipient]]]))
        solution = to_program(self.clvm, [self.inner_puzzle(self.state).tree_hash(), delegated])
        return LeafCall(
            Action("resolve", [h, upheld, sender]),
            [Message(sender, b"s" + sha256tree([st.dispute.root_id, upheld]))],
            extra_spends=[CoinSpend(bond_coin, self.bond_puzzle.serialize(), solution.serialize())],
        )

    def claim(self, h: int, campaign_id: bytes, puzzle_hash: bytes, cumulative: int, proof,
              auth: Auth = ByMessage(), excluded: Optional[Sequence[bytes]] = None) -> LeafCall:
        """`excluded`: the campaign's excluded list, which the leaf checks
        against the hash the campaign stored; default the one recorded at create."""
        leaf = claim_leaf(self.launcher_id, campaign_id, puzzle_hash, cumulative)
        fields, req = self._authorize(puzzle_hash, auth, leaf, claim=True)
        key = self.claim_key(campaign_id, puzzle_hash)
        insert, info, updates = self.claim_slot_info(key, cumulative)
        listed = list(self.excluded.get(campaign_id, []) if excluded is None else excluded)
        call = LeafCall(Action("claim", [h, campaign_id, puzzle_hash, cumulative, proof, insert, info, *fields, listed]),
                        claim_updates=updates)
        return self._with(call, req)

    def reclaim(self, h: int, campaign_id: bytes, auth: Auth = ByMessage()) -> LeafCall:
        st = self.chain_state.campaign(campaign_id)
        creator = st.creator if st else bytes(32)
        message = sha256tree(["flpcr-reclaim-v1", self.launcher_id, campaign_id])
        fields, req = self._authorize(creator, auth, message)
        return self._with(LeafCall(Action("reclaim", [h, campaign_id, *fields])), req)

    def delegate(self, h: int, owner: bytes, delegate: bytes, grant: int, auth: Auth = ByMessage()) -> LeafCall:
        message = sha256tree(["flpcr-delegate-v2", self.launcher_id, owner, delegate, grant])
        fields, req = self._authorize(owner, auth, message)
        key = self.delegation_key(owner, delegate)
        if key in self.delegations:
            rec = self.delegations[key]
            insert, info = 0, (rec.active, (rec.uses, (rec.left, rec.right)))
            updates = {key: (grant, rec.uses + 1, rec.left, rec.right)}
        else:
            lk, rk = neighbours(self.delegations, key)
            L, R = self.delegations[lk], self.delegations[rk]
            insert, info = 1, [lk, L.active, L.uses, L.left, rk, R.active, R.uses, R.right]
            updates = {
                lk: (L.active, L.uses, L.left, key),
                rk: (R.active, R.uses, key, R.right),
                key: (1, 0, lk, rk),
            }
        call = LeafCall(Action("delegate", [h, owner, delegate, grant, insert, info, *fields]),
                        fee=self.cfg.delegate_fee, delegation_updates=updates)
        return self._with(call, req)

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

    @staticmethod
    def _tagged(conditions: List[Program]) -> List[Program]:
        return [c.rest() for c in conditions
                if c.is_pair() and c.first().is_atom() and to_int(c.first()) == RESERVE_TAG]

    def _check_collisions(self, calls: Sequence[LeafCall], per_action: List[List[Program]]) -> None:
        seen: Set[Tuple[str, bytes]] = set()
        for call in calls:
            clash = seen & call.touched
            if clash:
                kind, key = sorted(clash)[0]
                raise BundleCollision(f"two actions touch the {kind} slot {key.hex()[:16]}; split them across spends")
            seen |= call.touched
        outputs: Set[Tuple[bytes, int]] = set()
        for conditions in per_action:
            for cond in self._tagged(conditions):
                items = to_list(cond)
                if to_int(items[0]) != OP_CREATE_COIN:
                    continue
                out = (to_bytes(items[1]), to_int(items[2]))
                if out in outputs:
                    raise BundleCollision(
                        f"two reserve payments of {out[1]} to {out[0].hex()[:16]} are one coin id; "
                        "split them across spends")
                outputs.add(out)

    def _action_layer_spend(self, actions: Sequence[Action]) -> CoinSpend:
        c = self.clvm
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
        singleton_solution = to_program(c, [self.lineage, 1, inner_solution])
        return CoinSpend(self.coin, self.full_puzzle(self.state).serialize(), singleton_solution.serialize())

    def _reserve_spends(self, new_state: Program, per_action: List[List[Program]],
                        cat_spends: Sequence[CatSpend]) -> List[CoinSpend]:
        """The reserve recreates itself at the new amount and runs every tagged condition."""
        c = self.clvm
        tagged: List[Program] = []
        for conditions in per_action:
            tagged.extend(reversed(self._tagged(conditions)))
        new_reserve = to_int(new_state.first())
        recreate = to_program(c, [OP_CREATE_COIN, self.reserve_ph, new_reserve, [self.reserve_ph]])
        delegated = c.pair(c.atom(b"\x01"), c.list([recreate, *tagged]))
        reserve_solution = to_program(c, [self.inner_puzzle(self.state).tree_hash(), delegated])
        if self.is_cat:
            c.spend_cats([CatSpend(self.reserve_cat, Spend(self.reserve_puzzle, reserve_solution)), *cat_spends])
            return list(c.coin_spends())
        if cat_spends:
            raise ValueError("an XCH distributor takes no CAT spends")
        return [CoinSpend(self.reserve_coin, self.reserve_puzzle.serialize(), reserve_solution.serialize())]

    def _slot_spends(self, per_action: List[List[Program]],
                     touched: Optional[Set[Tuple[str, bytes]]] = None) -> Tuple[List[CoinSpend], List[bytes]]:
        """Every slot a leaf spends: each SEND_MESSAGE mode 18 to a slot puzzle
        hash. Claim and delegation slots are different puzzles (nonces 1, 2).
        `touched` (the calls' slot keys) limits the records whose puzzles are
        built; every record is tried when a target is still unmatched."""
        c = self.clvm
        by_ph: Dict[bytes, Tuple[Program, bytes, bytes]] = {}

        def index(only: Optional[Set[Tuple[str, bytes]]]) -> None:
            for key, rec in self.claims.items():
                if only is None or ("claim", key) in only:
                    puzzle = self.claim_slot_puzzle(key, rec)
                    by_ph[puzzle.tree_hash()] = (puzzle, rec.creator_parent, rec.creator_inner_ph)
            for key, rec in self.delegations.items():
                if only is None or ("delegation", key) in only:
                    puzzle = self.delegation_slot_puzzle(key, rec)
                    by_ph[puzzle.tree_hash()] = (puzzle, rec.creator_parent, rec.creator_inner_ph)

        index(touched)
        if touched is not None:
            wanted = {to_bytes(to_list(cond)[3]) for conditions in per_action for cond in conditions
                      if cond.is_pair() and len(to_list(cond)) >= 4 and to_list(cond)[0].is_atom()
                      and to_int(to_list(cond)[0]) == OP_SEND_MESSAGE
                      and to_int(to_list(cond)[1]) == MODE_PUZZLE_TO_PUZZLE}
            if not wanted <= set(by_ph):
                index(None)
        inner_ph = self.inner_puzzle(self.state).tree_hash()
        spends, unresolved = [], []
        for conditions in per_action:
            for cond in conditions:
                items = to_list(cond) if cond.is_pair() else []
                if not (len(items) >= 4 and items[0].is_atom() and to_int(items[0]) == OP_SEND_MESSAGE
                        and to_int(items[1]) == MODE_PUZZLE_TO_PUZZLE):
                    continue
                target = to_bytes(items[3])
                if target not in by_ph:
                    unresolved.append(target)
                    continue
                puzzle, creator_parent, creator_inner = by_ph[target]
                creator = Coin(creator_parent, self.full_puzzle_hash_for_inner(creator_inner), 1)
                slot_coin = Coin(creator.coin_id(), target, 0)
                slot_solution = to_program(c, ((creator_parent, (creator_inner, 1)), inner_ph))
                spends.append(CoinSpend(slot_coin, puzzle.serialize(), slot_solution.serialize()))
        return spends, unresolved

    def prepare(self, calls: Sequence[LeafCall], cat_spends: Sequence[CatSpend] = ()) -> Prepared:
        """`cat_spends`: other coins of the reward CAT that must sit in the
        reserve's CAT ring, i.e. a creator's deposit. Raises the leaf's own
        error if an action is refused when run locally, and BundleCollision
        for actions that cannot share a spend."""
        if not calls:
            raise ValueError("a spend runs at least one action")
        actions = [call.action for call in calls]
        new_state, per_action = self.simulate(actions)
        self._check_collisions(calls, per_action)
        spends = [self._action_layer_spend(actions), *self._reserve_spends(new_state, per_action, cat_spends)]
        touched: Set[Tuple[str, bytes]] = set()
        for call in calls:
            touched |= call.touched
        slot_spends, unresolved = self._slot_spends(per_action, touched)
        claim_updates: Dict[bytes, ClaimUpdate] = {}
        delegation_updates: Dict[bytes, DelegationUpdate] = {}
        for call in calls:
            claim_updates.update(call.claim_updates)
            delegation_updates.update(call.delegation_updates)
        for call in calls:
            spends.extend(call.extra_spends)
        return Prepared(
            spends=spends + slot_spends,
            new_state=new_state,
            per_action=per_action,
            messages=[m for call in calls for m in call.messages],
            signatures=[s for call in calls for s in call.signatures],
            fee=sum(call.fee for call in calls),
            creations=[x for call in calls for x in call.creations],
            claim_updates=claim_updates,
            delegation_updates=delegation_updates,
            unresolved=unresolved,
        )

    def apply(self, prepared: Prepared) -> None:
        """Advance to the generation `prepared` produced, once it has confirmed."""
        old_coin = self.coin
        old_inner_ph = self.inner_puzzle(self.state).tree_hash()
        parent = old_coin.parent_coin_info
        for key, value in prepared.claim_updates.items():
            if value is None:
                self.claims.pop(key, None)
            else:
                claimed, left, right = value
                self.claims[key] = ClaimRecord(claimed, left, right, parent, old_inner_ph)
        for key, (active, uses, left, right) in prepared.delegation_updates.items():
            self.delegations[key] = DelegationRecord(active, uses, left, right, parent, old_inner_ph)
        self.lineage = [parent, old_inner_ph, 1]
        self.state = prepared.new_state
        self.coin = Coin(old_coin.coin_id(), self.full_puzzle(prepared.new_state).tree_hash(), 1)
        new_reserve = to_int(prepared.new_state.first())
        if self.is_cat:
            self.reserve_cat = self.reserve_cat.child(self.reserve_ph, new_reserve)
            self.reserve_coin = self.reserve_cat.coin
        else:
            self.reserve_coin = Coin(self.reserve_coin.coin_id(), self.reserve_ph, new_reserve)

    # --- sequencing ------------------------------------------------------------------------

    def _mark(self):
        return (self.state, self.coin, self.lineage, self.reserve_coin, getattr(self, "reserve_cat", None),
                dict(self.claims), dict(self.delegations))

    def _rewind(self, mark) -> None:
        (self.state, self.coin, self.lineage, self.reserve_coin, reserve_cat,
         self.claims, self.delegations) = mark[0], mark[1], mark[2], mark[3], mark[4], dict(mark[5]), dict(mark[6])
        if reserve_cat is not None:
            self.reserve_cat = reserve_cat

    def spend_cost(self, spends: Sequence[CoinSpend]) -> int:
        """What consensus charges these spends: CLVM execution, 12,000 per
        byte of puzzle and solution, 1,800,000 per CREATE_COIN and 1,200,000
        per AGG_SIG_* condition. Run in a throwaway allocator: the SDK's
        allocator never frees, and the sequencer costs every generation."""
        c = Clvm()
        total = 0
        for s in spends:
            out = c.deserialize(s.puzzle_reveal).run(c.deserialize(s.solution), MAX_COST, True)
            total += out.cost + 12_000 * (len(s.puzzle_reveal) + len(s.solution))
            for cond in to_list(out.value):
                op = to_list(cond)[0] if cond.is_pair() else None
                code = to_int(op) if op is not None and op.is_atom() else None
                if code == OP_CREATE_COIN:
                    total += 1_800_000
                elif code is not None and 43 <= code <= 50:
                    total += 1_200_000
        return total

    def sequence(self, h: int, requests: Sequence[ClaimRequest], max_cost: int = 5_000_000_000,
                 per_generation: int = 25) -> ClaimBatch:
        """Pack `requests` into one bundle of chained generations. Each
        generation takes every request whose slots no earlier request in it
        touches (first claims next to each other share a neighbour, so they
        go to the next generation, where that neighbour has been recreated),
        up to `per_generation`. Generations are added while the bundle's
        cost (`spend_cost`) stays under `max_cost`; the rest is `deferred`. One request
        per (campaign, holder): the highest cumulative wins. The driver is left
        where it was: apply the batch with `apply_batch` once it confirms."""
        best: Dict[bytes, ClaimRequest] = {}
        for r in requests:
            key = self.claim_key(r.campaign_id, r.puzzle_hash)
            if key not in best or r.cumulative > best[key].cumulative:
                best[key] = r
        queue = list(best.values())
        mark = self._mark()
        generations: List[Prepared] = []
        placed: List[ClaimRequest] = []
        rejected: List[Tuple[ClaimRequest, str]] = []
        cost = 0
        try:
            while queue:
                calls, taken, later = [], [], []
                truth = self.clvm.pair(self.clvm.nil(), self.state)  # run each claim once, on the one before
                seen: Set[Tuple[str, bytes]] = set()
                payments: Set[Tuple[bytes, int]] = set()
                for r in queue:
                    if len(calls) >= per_generation:
                        later.append(r)
                        continue
                    try:
                        call = self.claim(h, r.campaign_id, r.puzzle_hash, r.cumulative, r.proof, r.auth)
                    except (ValueError, KeyError) as e:
                        rejected.append((r, str(e)))
                        continue
                    previous = self.claims.get(self.claim_key(r.campaign_id, r.puzzle_hash))
                    payment = (r.puzzle_hash, r.cumulative - (previous.claimed if previous else 0))
                    if call.touched & seen or payment in payments:
                        later.append(r)
                        continue
                    try:  # the leaf's own verdict on this claim, after the ones already taken
                        truth, _ = self.run_leaf(call.action.leaf, truth, call.action.solution)
                    except ValueError as e:
                        rejected.append((r, str(e)))
                        continue
                    calls.append(call)
                    taken.append(r)
                    seen |= call.touched
                    payments.add(payment)
                if not calls:
                    break
                prepared = self.prepare(calls)
                step = self.spend_cost(prepared.spends)
                if generations and cost + step > max_cost:
                    later = taken + later
                    queue = later
                    break
                cost += step
                generations.append(prepared)
                placed += taken
                self.apply(prepared)
                queue = later
        finally:
            self._rewind(mark)
        return ClaimBatch(generations, placed, queue, rejected, cost)

    def reloaded(self) -> "Distributor":
        """The same distributor in a fresh allocator. The SDK's allocator never
        frees, so a long-running sequencer swaps to a reloaded copy between
        bundles (XCH distributors: `snapshot` does not carry a CAT lineage)."""
        fresh = Distributor(Puzzles(Clvm()), self.cfg, self.chain_state.engine_ph)  # restore replaces the state
        fresh.restore(self.snapshot())
        return fresh

    def apply_batch(self, batch: ClaimBatch) -> None:
        """Advance through every generation of a confirmed batch."""
        for prepared in batch.generations:
            self.apply(prepared)

    # --- persistence --------------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Everything `apply` keeps, as JSON-safe values, so a later process can
        carry on from the same generation. XCH distributors only for now: a CAT
        reserve's lineage proof is not serialized yet."""
        if self.is_cat:
            raise NotImplementedError("snapshot of a CAT distributor is not supported yet")
        if self.coin is None:
            raise ValueError("nothing to snapshot before launch")

        def coin(c: Coin) -> dict:
            return {"parent_coin_info": c.parent_coin_info.hex(), "puzzle_hash": c.puzzle_hash.hex(), "amount": c.amount}

        def record(r) -> dict:
            out = {k: (v.hex() if isinstance(v, bytes) else v) for k, v in vars(r).items()}
            return out

        return {
            "launcher_id": self.launcher_id.hex(),
            "state": self.state.serialize().hex(),
            "coin": coin(self.coin),
            "lineage": [x.hex() if isinstance(x, bytes) else x for x in self.lineage],
            "reserve_coin": coin(self.reserve_coin),
            "claims": {k.hex(): record(v) for k, v in self.claims.items()},
            "delegations": {k.hex(): record(v) for k, v in self.delegations.items()},
            "excluded": {k.hex(): [x.hex() for x in v] for k, v in self.excluded.items()},
        }

    def restore(self, snap: dict) -> None:
        if bytes.fromhex(snap["launcher_id"]) != self.launcher_id:
            raise ValueError("snapshot is of another distributor")

        def coin(d: dict) -> Coin:
            return Coin(bytes.fromhex(d["parent_coin_info"]), bytes.fromhex(d["puzzle_hash"]), int(d["amount"]))

        def unhex(d: dict) -> dict:
            return {k: (bytes.fromhex(v) if isinstance(v, str) else v) for k, v in d.items()}

        self.state = self.clvm.deserialize(bytes.fromhex(snap["state"]))
        self.coin = coin(snap["coin"])
        self.lineage = [bytes.fromhex(x) if isinstance(x, str) else x for x in snap["lineage"]]
        self.reserve_coin = coin(snap["reserve_coin"])
        self.claims = {bytes.fromhex(k): ClaimRecord(**unhex(v)) for k, v in snap["claims"].items()}
        self.delegations = {bytes.fromhex(k): DelegationRecord(**unhex(v)) for k, v in snap["delegations"].items()}
        self.excluded = {bytes.fromhex(k): [bytes.fromhex(x) for x in v] for k, v in snap.get("excluded", {}).items()}

    # --- slot list helpers ------------------------------------------------------------------

    @staticmethod
    def claim_key(campaign_id: bytes, puzzle_hash: bytes) -> bytes:
        return sha256tree((campaign_id, puzzle_hash))

    @staticmethod
    def delegation_key(owner: bytes, delegate: bytes) -> bytes:
        return sha256tree((owner, delegate))

    def is_delegated(self, owner: bytes, delegate: bytes) -> bool:
        rec = self.delegations.get(self.delegation_key(owner, delegate))
        return bool(rec and rec.active)

    def claim_slot_info(self, key: bytes, cumulative: int):
        """(insert flag, slot_info, claim updates to apply once confirmed)."""
        if key in self.claims:
            rec = self.claims[key]
            return 0, (rec.claimed, (rec.left, rec.right)), {key: (cumulative, rec.left, rec.right)}
        lk, rk = neighbours(self.claims, key)
        L, R = self.claims[lk], self.claims[rk]
        info = [lk, L.claimed, L.left, rk, R.claimed, R.right]
        updates = {
            lk: (L.claimed, L.left, key),
            rk: (R.claimed, key, R.right),
            key: (cumulative, lk, rk),
        }
        return 1, info, updates


__all__ = [
    "Action",
    "BundleCollision",
    "ClaimBatch",
    "ClaimRequest",
    "ClaimRecord",
    "DelegationRecord",
    "Distributor",
    "HEIGHT_WINDOW",
    "LeafCall",
    "Prepared",
    "Puzzles",
]
