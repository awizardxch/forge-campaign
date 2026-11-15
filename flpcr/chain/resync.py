"""Rebuild a distributor's driver state from the chain alone.

Until now the deploy record was the only way to spend a distributor again: lose
or corrupt it and no root could be posted, no claim built, nothing reclaimed.
`resync` needs only the launcher id (and the leaves the distributor was built
with):

1. The launcher's parent is the funding coin; its child is the eve singleton.
2. The first singleton spend reveals the leaves it ran, and every leaf is
   curried with the same Config, so the Config comes from chain too. The
   rebuilt Config must hash to the revealed one, or these leaves are not the
   ones the distributor was launched with.
3. Each spend reveals the singleton's puzzle (the state is curried into it)
   and the actions it ran (leaf puzzles, CHIP-0050 selectors, run on the revealed
   leaves as the action layer runs them, and solutions). Every action is run again, and the resulting puzzle hash must
   be the next singleton coin's: a wrong replay cannot go one generation
   without failing.
4. Claim and delegation slot records are rebuilt from the values each action
   revealed (`insert`, the neighbours or the slot's own value, a delegated
   use), whoever built the bundle. At the end every record's slot coin must
   exist on chain unspent.

The result is the same `Distributor` a live driver holds, plus what the chain
says about campaigns (the create solutions) and roots (the post_root solutions).
Root proofs are not on chain: the engine recomputes them deterministically.

`reader` is the three-call `ChainReader` of `flpcr.chain.nfts`: the simulator,
or a `NodeReader` over a full node or coinset.org. The reward asset may be XCH
or a CAT: the first spend names the reserve's parent, and for a CAT that
parent's spend yields the reserve with the lineage its next spend needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from chia_wallet_sdk import Clvm, Coin, Constants, Program

from ..clvm import sha256tree
from ..distributor import Config
from ..leaves import LEAVES
from .auth import AUTH_DELEGATE
from .driver import FINALIZER_PINNED, FINALIZER_UPSTREAM, HIGH, LOW, MAX_COST, Distributor, Prepared, Puzzles
from .state import ChainState
from .values import to_bytes, to_int, to_list


class ResyncError(ValueError):
    """The chain does not replay to what these leaves produce."""


@dataclass
class ReplayedAction:
    height: int  # the block the spend confirmed in
    leaf: str
    solution: Program


@dataclass
class Resynced:
    distributor: Distributor
    config: Config
    funding_coin: bytes
    generations: int
    actions: List[ReplayedAction] = field(default_factory=list)
    campaigns: Dict[str, dict] = field(default_factory=dict)  # id -> what its create solution says
    roots: Dict[str, dict] = field(default_factory=dict)  # root id -> start, end, root, totals, posted at
    # Generations whose spend named a reserve other than the replayed one (upstream finalizer
    # only, CP-06): (generation, height, the reserve coin id the chain followed instead).
    reserve_forks: List[Tuple[int, int, str]] = field(default_factory=list)
    # The State the eve coin was born with, as its first spend revealed it. A distributor of the
    # pinned build can be launched with any State (the launch is the launcher's, not a leaf's), so
    # this is what BuildPin.check holds against ChainState.genesis: a launch that starts with
    # campaign rows in it is refused (2026-11-14 conformance review, part B, F-B1).
    initial_state: Optional[ChainState] = None
    # Its tree hash exactly as revealed: two spellings that parse to the same ChainState (a 0x00
    # where nil belongs, a ninth element) still differ here, so "born empty" is byte-exact.
    initial_state_hash: Optional[bytes] = None


def _config_from_value(v: List[Program]) -> Config:
    ints = lambda i: to_int(v[i])  # noqa: E731
    raw = lambda i: to_bytes(v[i])  # noqa: E731
    return Config(raw(0), raw(1), raw(3), raw(4), raw(5), raw(6), fee_bps=ints(2), dispute_blocks=ints(7),
                  rotate_delay=ints(8), delegate_fee=ints(9), min_campaign_length=ints(10),
                  max_campaign_length=ints(11), max_campaigns=ints(12), dispute_bond=ints(15))


def _finalizer_kind(clvm: Clvm, spend) -> str:
    """Which reserve finalizer the spent coin runs: the pinned one is curried a second time with
    the reserve's parent (two arguments), upstream's with its own hash only (one)."""
    inner = clvm.deserialize(spend.puzzle_reveal).uncurry().args[1].uncurry()
    if inner is None:
        raise ResyncError("the singleton's inner puzzle is not an action layer")
    finalizer = inner.args[0].uncurry()
    n = len(finalizer.args) if finalizer is not None else 0
    if n == 2:
        return FINALIZER_PINNED
    if n == 1:
        return FINALIZER_UPSTREAM
    raise ResyncError("the action layer's finalizer is neither reserve finalizer this build knows")


def _reserve_parent(clvm: Clvm, spend, kind: str) -> bytes:
    """The parent of the reserve coin a singleton spend messages. The pinned finalizer carries
    it curried into the coin's own puzzle (bound by consensus to the coin); upstream's reads it
    from the action layer's solution, so ANY coin at the reserve's puzzle hash holding the
    state's amount can be named there (audit 2026-11-11, B2 CP-06)."""
    if kind == FINALIZER_PINNED:
        inner = clvm.deserialize(spend.puzzle_reveal).uncurry().args[1].uncurry()
        return to_bytes(inner.args[0].uncurry().args[1])
    inner_solution = to_list(clvm.deserialize(spend.solution))[2]
    return to_bytes(inner_solution.rest().rest().rest())


def _reserve_at(reader, clvm: Clvm, d: Distributor, parent_id: bytes, amount: int) -> None:
    """Point `d` at the reserve coin born from `parent_id` holding `amount`: for a CAT, with the
    lineage its next spend needs, read from the parent's own spend."""
    reserve = Coin(parent_id, d.reserve_full_ph, amount)
    if not d.is_cat:
        d.reserve_coin = reserve
        return
    parent = reader.coin_state(parent_id)
    parent_spend = reader.coin_spend(parent_id)
    if parent is None or parent_spend is None:
        raise ResyncError("the CAT reserve's parent is not on chain")
    puzzle, solution = clvm.deserialize(parent_spend.puzzle_reveal).puzzle(), clvm.deserialize(parent_spend.solution)
    cats = puzzle.parse_child_cats(parent.coin, solution) or []
    match = [c for c in cats if c.coin.coin_id() == reserve.coin_id()]
    if not match:
        raise ResyncError("the CAT reserve's parent did not create this distributor's reserve")
    d.reserve_cat = match[0]
    d.reserve_coin = d.reserve_cat.coin


def _decode(clvm: Clvm, spend) -> Tuple[Program, List[Tuple[Program, Program]]]:
    """(state before the spend, [(leaf puzzle, leaf solution)] in the order they ran)."""
    puzzle = clvm.deserialize(spend.puzzle_reveal)
    solution = clvm.deserialize(spend.solution)
    singleton = puzzle.uncurry()
    if singleton is None or len(singleton.args) != 2:
        raise ResyncError("not a singleton spend")
    inner = singleton.args[1].uncurry()
    if inner is None or len(inner.args) != 3:
        raise ResyncError("the singleton's inner puzzle is not an action layer")
    state = inner.args[2]
    inner_solution = to_list(solution)[2]
    leaves = inner_solution.first()
    selectors = to_list(inner_solution.rest().first())
    solutions = to_list(inner_solution.rest().rest().first())
    if len(selectors) != len(solutions):
        raise ResyncError("selectors and solutions do not pair up")
    # The builder lists selectors in reverse action order (driver._action_layer_spend).
    runs = [(_select(leaves, sel.first()), sol) for sel, sol in zip(reversed(selectors), solutions)]
    return state, runs


def _select(leaves: Program, selector: Program) -> Program:
    """The leaf a selector picks, found the way the action layer finds it: by RUNNING the
    selector with the revealed puzzle list as its environment (`selector(...puzzles)`).

    Our driver writes paths (2, 5, 11, ...), and a path atom run on the list is that path, but
    the puzzle enforces no form: `(q . leaf)` returns the leaf whatever the list holds, and the
    chain accepts it (audit 2026-11-11, CE-13: `to_int` of a pair raised, so one such claim made
    every later boot fail). A selector reused without a proof (`item_in_list`) is an earlier
    entry's selector, so it runs the same way. The caller then names the leaf by its tree hash,
    so a selector that returns anything but one of this distributor's leaves is refused there.
    """
    try:
        return selector.run(leaves, MAX_COST, False).value
    except Exception as exc:  # noqa: BLE001 -- a selector the action layer could not have run either
        raise ResyncError("a spend names a leaf selector that does not run on its revealed leaves") from exc


def _is_zero(program: Program) -> bool:
    """CLVM zero is the empty atom and nothing else: what a leaf's `x == 0` tests."""
    return to_bytes(program) == b""


def _slot_updates(d: Distributor, leaf: str, sol: List[Program]) -> Tuple[dict, dict]:
    """The claim and delegation slot records an action leaves behind, from its revealed values."""
    claims: dict = {}
    delegations: dict = {}

    def delegated(auth: Program, data: Program, owner: bytes) -> None:
        if to_bytes(auth) != bytes([AUTH_DELEGATE]):  # an atom compare, as in the leaf
            return
        delegate = to_bytes(data.first())
        rest = data.rest()
        uses, left, right = to_int(rest.first()), to_bytes(rest.rest().first()), to_bytes(rest.rest().rest().first())
        delegations[d.delegation_key(owner, delegate)] = (1, uses + 1, left, right)

    if leaf == "init":
        claims = {LOW: (0, LOW, HIGH), HIGH: (0, LOW, HIGH)}
        delegations = {LOW: (0, 0, LOW, HIGH), HIGH: (0, 0, LOW, HIGH)}
    elif leaf == "claim":
        campaign_id, ph, cumulative = to_bytes(sol[1]), to_bytes(sol[2]), to_int(sol[3])
        info = sol[6]
        key = d.claim_key(campaign_id, ph)
        # The leaf compares atoms (`insert == 0` is true only for the empty atom), so a one-byte
        # 0x00 takes the insert branch on chain. Branch on the raw atom, exactly as it does
        # (audit 2026-11-10, CE-02: `to_int` read 0x00 as 0 and resync raised for good).
        if _is_zero(sol[5]):
            claims[key] = (cumulative, to_bytes(info.rest().first()), to_bytes(info.rest().rest()))
        else:
            n = to_list(info)
            lk, lc, ll, rk, rc, rr = to_bytes(n[0]), to_int(n[1]), to_bytes(n[2]), to_bytes(n[3]), to_int(n[4]), to_bytes(n[5])
            claims = {lk: (lc, ll, key), rk: (rc, key, rr), key: (cumulative, lk, rk)}
        delegated(sol[7], sol[8], ph)
    elif leaf == "delegate":
        owner, delegate, grant = to_bytes(sol[1]), to_bytes(sol[2]), to_int(sol[3])
        info = sol[5]
        key = d.delegation_key(owner, delegate)
        if _is_zero(sol[4]):  # the leaf's own test: the empty atom (CE-02)
            rest = info.rest()
            uses, left, right = to_int(rest.first()), to_bytes(rest.rest().first()), to_bytes(rest.rest().rest())
            delegations[key] = (grant, uses + 1, left, right)
        else:
            n = to_list(info)
            lk, la, lu, ll = to_bytes(n[0]), to_int(n[1]), to_int(n[2]), to_bytes(n[3])
            rk, ra, ru, rr = to_bytes(n[4]), to_int(n[5]), to_int(n[6]), to_bytes(n[7])
            delegations = {lk: (la, lu, ll, key), rk: (ra, ru, key, rr), key: (1, 0, lk, rk)}
    elif leaf == "reclaim":
        campaign = d.chain_state.campaign(to_bytes(sol[1]))
        if campaign is not None:
            delegated(sol[2], sol[3], campaign.creator)
    return claims, delegations


def resync(reader, puzzles: Puzzles, launcher_id: bytes) -> Resynced:
    clvm = puzzles.clvm
    launcher = reader.coin_state(launcher_id)
    if launcher is None:
        raise ResyncError(f"launcher {launcher_id.hex()} is not on chain")
    funding = launcher.coin.parent_coin_info
    eves = [c for c in reader.children(launcher_id) if c.coin.amount % 2 == 1]
    if not eves:
        raise ResyncError("the launcher has no singleton child")
    coin = eves[0].coin
    first = reader.coin_spend(coin.coin_id())
    if first is None:
        raise ResyncError("the distributor was launched but never spent; nothing reveals its Config yet")

    # The Config, from any leaf the first spend revealed.
    _, runs = _decode(clvm, first)
    curried = runs[0][0].uncurry()
    if curried is None or len(curried.args) != 1:
        raise ResyncError("the first action's leaf is not a curried FLPCR leaf")
    config_program = curried.args[0]
    cfg = _config_from_value(to_list(config_program))
    if cfg.distributor_id != launcher_id:
        raise ResyncError("the revealed Config names another distributor")
    state0, _ = _decode(clvm, first)
    kind = _finalizer_kind(clvm, first)
    if kind not in puzzles.mods:
        raise ResyncError(f"the distributor runs {kind}, which these compiled puzzles do not contain")
    d = Distributor(puzzles, cfg, ChainState.from_program(state0).engine_ph, finalizer=kind)
    if sha256tree(d.config_value()) != config_program.tree_hash():
        raise ResyncError("these leaves curry a different Config than the distributor was launched with "
                          "(another build of the puzzles): resync with the matching compiled folder")
    names = {h: n for n, h in d.leaf_hashes.items()}
    unknown = [p for p, _ in runs if p.tree_hash() not in names]
    if unknown:
        raise ResyncError("the distributor ran a leaf these compiled puzzles do not contain: it was launched "
                          "with another build of the leaves. Resync with that build (FLPCR_COMPILED pointing "
                          "at its compiled folder)")

    d.state = state0
    d.coin = coin
    d.lineage = [funding, 1]
    # The empty reserve, created at launch: by the funding coin for XCH, by a seed coin of the
    # reward CAT for a CAT. The first spend names its parent either way (curried into the eve
    # under the pinned finalizer). Under upstream's, the first spend (a permissionless `init`)
    # may name a decoy instead; the chain then follows the decoy, and so does the replay.
    _reserve_at(reader, clvm, d, _reserve_parent(clvm, first, kind), ChainState.from_program(state0).reserve)
    if d.full_puzzle(d.state).tree_hash() != coin.puzzle_hash:
        raise ResyncError("the eve coin's puzzle is not what these compiled leaves build (the leaf set's merkle "
                          "root differs): the distributor was launched with another build of the leaves. Resync "
                          "with that build (FLPCR_COMPILED pointing at its compiled folder)")
    out = Resynced(d, cfg, funding, 0, initial_state=ChainState.from_program(state0),
                   initial_state_hash=bytes(state0.tree_hash()))

    while True:
        spend = reader.coin_spend(d.coin.coin_id())
        if spend is None:
            break  # the current generation
        height = reader.coin_state(d.coin.coin_id()).spent_height
        state, runs = _decode(clvm, spend)
        if state.tree_hash() != d.state.tree_hash():
            raise ResyncError(f"generation {out.generations}: the revealed state is not the replayed one")
        # The reserve this spend messaged is the reserve, whatever the replay expected: the spend
        # confirmed, so that coin received the singleton's message. Upstream's finalizer lets a
        # solution name any coin at the reserve's puzzle hash holding the state's amount (CP-06);
        # follow it rather than refuse to boot. Under the pinned finalizer the parent is part of
        # the coin's own puzzle, so a mismatch means the replay itself is wrong.
        named = _reserve_parent(clvm, spend, kind)
        if named != d.reserve_coin.parent_coin_info:
            if kind == FINALIZER_PINNED:
                raise ResyncError(f"generation {out.generations}: the coin's pinned reserve is not the replayed one")
            _reserve_at(reader, clvm, d, named, d.chain_state.reserve)
            out.reserve_forks.append((out.generations, height, d.reserve_coin.coin_id().hex()))
        truth = clvm.pair(clvm.nil(), d.state)
        claim_updates: dict = {}
        delegation_updates: dict = {}
        for leaf_puzzle, leaf_solution in runs:
            name = names.get(leaf_puzzle.tree_hash())
            if name is None:
                raise ResyncError(f"generation {out.generations}: a leaf that is not one of this distributor's")
            sol = to_list(leaf_solution)
            c_up, d_up = _slot_updates(d, name, sol)
            # Later actions in one spend see the earlier ones' records (the builder chains them).
            claim_updates.update(c_up)
            delegation_updates.update(d_up)
            ran = d.leaves[name].run(clvm.pair(truth, leaf_solution), MAX_COST, False)
            truth = ran.value.first()
            out.actions.append(ReplayedAction(height, name, leaf_solution))
            _note(out, name, sol, height)
        new_state = truth.rest()
        nxt = Coin(d.coin.coin_id(), d.successor_puzzle_hash(new_state), 1)
        children = {c.coin.coin_id() for c in reader.children(d.coin.coin_id())}
        if nxt.coin_id() not in children:
            raise ResyncError(f"generation {out.generations}: the replay does not create the next singleton coin")
        d.apply(Prepared(spends=[], new_state=new_state, per_action=[], messages=[], signatures=[], fee=0,
                         claim_updates=claim_updates, delegation_updates=delegation_updates))
        out.generations += 1

    _check_slots(reader, d)
    reserve = reader.coin_state(d.reserve_coin.coin_id())
    if reserve is None or reserve.spent_height is not None:
        raise ResyncError("the replayed reserve coin is not the unspent reserve on chain")
    return out


def _list_hash(item_hashes: List[bytes]) -> bytes:
    from hashlib import sha256
    acc = sha256(b"\x01").digest()  # nil
    for h in reversed(item_hashes):
        acc = sha256(b"\x02" + h + acc).digest()
    return acc


def _note(out: Resynced, leaf: str, sol: List[Program], height: int) -> None:
    """What the deploy record keeps: excluded lists, campaign fields, posted roots."""
    d = out.distributor
    if leaf == "create":
        creator, amount, start, end, kind = to_bytes(sol[1]), to_int(sol[2]), to_int(sol[3]), to_int(sol[4]), to_int(sol[5])
        target, params, nonce, window = to_bytes(sol[6]), sol[7], sol[8], to_int(sol[9])
        params_list = to_list(params) if params.is_pair() else []
        excluded = [to_bytes(x) for x in to_list(params_list[3])] if len(params_list) > 3 and params_list[3].is_pair() else []
        # create.rue's id: the tree hash of this list. The nonce is any CLVM value, so the list
        # hash is built from item hashes (its own tree hash, every other item's as an atom).
        items = ["flpcr-campaign-v1", d.launcher_id, creator, d.cfg.reward_asset, amount, start, end, kind,
                 target, params.tree_hash(), nonce, d.cfg.fee_bps, window]
        cid = _list_hash([x.tree_hash() if isinstance(x, Program) else sha256tree(x) for x in items])
        d.excluded[cid] = excluded
        out.campaigns[cid.hex()] = {"id": cid.hex(), "creator": creator.hex(), "amount": amount, "start": start,
                                    "end": end, "claim_window": window, "kind": kind, "target": target.hex(),
                                    "nonce": to_int(nonce) if nonce.is_atom() else nonce.tree_hash().hex(),
                                    "excluded": [x.hex() for x in excluded], "created_at": height,
                                    "params": params.serialize().hex()}
    elif leaf == "post_root":
        root_id = to_int(sol[1])
        totals = {to_bytes(t.first()).hex(): to_int(t.rest()) for t in to_list(sol[5])} if sol[5].is_pair() else {}
        manifest = to_bytes(sol[6]).hex() if len(sol) > 6 else bytes(32).hex()
        out.roots[str(root_id)] = {"start": to_int(sol[2]), "end": to_int(sol[3]), "root": to_bytes(sol[4]).hex(),
                                   "totals": totals, "manifest_hash": manifest, "posted_at": height}


def _check_slots(reader, d: Distributor) -> None:
    """Every rebuilt slot record must be an unspent slot coin on chain."""
    for kind, records, puzzle_of in (("claim", d.claims, d.claim_slot_puzzle),
                                     ("delegation", d.delegations, d.delegation_slot_puzzle)):
        for key, rec in records.items():
            creator = Coin(rec.creator_parent, d.full_puzzle_hash_for_inner(rec.creator_inner_ph), 1)
            slot = Coin(creator.coin_id(), puzzle_of(key, rec).tree_hash(), 0)
            state = reader.coin_state(slot.coin_id())
            if state is None or state.spent_height is not None:
                raise ResyncError(f"{kind} slot {key.hex()[:12]} is not an unspent coin on chain")
