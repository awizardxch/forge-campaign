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
   and the actions it ran (leaf puzzles, CHIP-0050 selectors as CLVM paths,
   solutions). Every action is run again, and the resulting puzzle hash must
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
from .driver import HIGH, LOW, MAX_COST, Distributor, Prepared, Puzzles
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


def _path(node: Program, path: int) -> Program:
    """A CLVM path: from the least significant bit, 0 takes first and 1 takes rest, until 1 is left."""
    if path < 1:
        raise ResyncError(f"bad selector {path}")
    while path > 1:
        node = node.rest() if path & 1 else node.first()
        path >>= 1
    return node


def _config_from_value(v: List[Program]) -> Config:
    ints = lambda i: to_int(v[i])  # noqa: E731
    raw = lambda i: to_bytes(v[i])  # noqa: E731
    return Config(raw(0), raw(1), raw(3), raw(4), raw(5), raw(6), fee_bps=ints(2), dispute_blocks=ints(7),
                  rotate_delay=ints(8), delegate_fee=ints(9), min_campaign_length=ints(10),
                  max_campaign_length=ints(11), max_campaigns=ints(12), dispute_bond=ints(15))


def _reserve_parent(clvm: Clvm, spend) -> bytes:
    """The reserve coin's parent, which every singleton spend names in the action layer's solution."""
    inner_solution = to_list(clvm.deserialize(spend.solution))[2]
    return to_bytes(inner_solution.rest().rest().rest())


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
    runs = [(_path(leaves, to_int(sel.first())), sol) for sel, sol in zip(reversed(selectors), solutions)]
    return state, runs


def _slot_updates(d: Distributor, leaf: str, sol: List[Program]) -> Tuple[dict, dict]:
    """The claim and delegation slot records an action leaves behind, from its revealed values."""
    claims: dict = {}
    delegations: dict = {}

    def delegated(auth: Program, data: Program, owner: bytes) -> None:
        if to_int(auth) != AUTH_DELEGATE:
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
        insert, info = to_int(sol[5]), sol[6]
        key = d.claim_key(campaign_id, ph)
        if insert == 0:
            claims[key] = (cumulative, to_bytes(info.rest().first()), to_bytes(info.rest().rest()))
        else:
            n = to_list(info)
            lk, lc, ll, rk, rc, rr = to_bytes(n[0]), to_int(n[1]), to_bytes(n[2]), to_bytes(n[3]), to_int(n[4]), to_bytes(n[5])
            claims = {lk: (lc, ll, key), rk: (rc, key, rr), key: (cumulative, lk, rk)}
        delegated(sol[7], sol[8], ph)
    elif leaf == "delegate":
        owner, delegate, grant = to_bytes(sol[1]), to_bytes(sol[2]), to_int(sol[3])
        insert, info = to_int(sol[4]), sol[5]
        key = d.delegation_key(owner, delegate)
        if insert == 0:
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
    d = Distributor(puzzles, cfg, ChainState.from_program(state0).engine_ph)
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
    # reward CAT for a CAT. The first spend names its parent either way.
    reserve_parent = _reserve_parent(clvm, first)
    reserve = Coin(reserve_parent, d.reserve_full_ph, 0)
    if d.is_cat:
        parent = reader.coin_state(reserve_parent)
        parent_spend = reader.coin_spend(reserve_parent)
        if parent is None or parent_spend is None:
            raise ResyncError("the CAT reserve's parent is not on chain")
        puzzle, solution = clvm.deserialize(parent_spend.puzzle_reveal).puzzle(), clvm.deserialize(parent_spend.solution)
        cats = puzzle.parse_child_cats(parent.coin, solution) or []
        match = [c for c in cats if c.coin.coin_id() == reserve.coin_id()]
        if not match:
            raise ResyncError("the CAT reserve's parent did not create this distributor's reserve")
        d.reserve_cat = match[0]
        d.reserve_coin = d.reserve_cat.coin
    else:
        if reserve_parent != funding:
            raise ResyncError("the first spend names a reserve the launch did not create")
        d.reserve_coin = reserve
    if d.full_puzzle(d.state).tree_hash() != coin.puzzle_hash:
        raise ResyncError("the eve coin's puzzle is not what these compiled leaves build (the leaf set's merkle "
                          "root differs): the distributor was launched with another build of the leaves. Resync "
                          "with that build (FLPCR_COMPILED pointing at its compiled folder)")
    out = Resynced(d, cfg, funding, 0)

    while True:
        spend = reader.coin_spend(d.coin.coin_id())
        if spend is None:
            break  # the current generation
        height = reader.coin_state(d.coin.coin_id()).spent_height
        state, runs = _decode(clvm, spend)
        if state.tree_hash() != d.state.tree_hash():
            raise ResyncError(f"generation {out.generations}: the revealed state is not the replayed one")
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
        nxt = Coin(d.coin.coin_id(), d.full_puzzle(new_state).tree_hash(), 1)
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
        out.roots[str(root_id)] = {"start": to_int(sol[2]), "end": to_int(sol[3]), "root": to_bytes(sol[4]).hex(),
                                   "totals": totals, "posted_at": height}


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
