"""The hosted campaign service: one distributor, rebuilt from chain, kept current.

Boot needs no state file:

1. `resync` rebuilds the driver (coin, state, reserve, every slot) from the
   launcher id.
2. The engine reruns every posted root from the same sources the operator used.
   Each rebuilt root must hash to the one on chain, or the service refuses to
   serve proofs from that root on: a holder is never handed a proof the chain
   would refuse.

Then:

* `status()` and `claimable(puzzle_hash)` answer the public API: campaigns,
  roots, and each holder's leaves with the exact message to sign.
* `operator_step()` posts a root when one is due, signed with the operator key
  the service holds (its only key: the guardian and admin stay multisigs).
* the sequencer (`flpcr.service.sequencer`) batches claimants' signed claims.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from chia_wallet_sdk import Clvm, Coin, SecretKey, standard_puzzle_hash

from ..chain.driver import HEIGHT_WINDOW, Distributor, Puzzles
from ..chain.resync import Resynced, resync
from ..chain.signer import KeySigner
from ..chain.values import to_bytes, to_int, to_list
from ..engine import EngineState, Root, run_epoch
from ..merkle import claim_leaf
from ..model import Campaign, Kind, Params

MIN_FEE = 2_000_000_000  # 0.002 XCH: the floor the deploy scripts use
FEE_PER_COST = 10  # mojos per unit of cost; under 5 the mempool refuses a nonzero fee


def params_from_program(clvm: Clvm, hex_value: str) -> Params:
    """A campaign's Params from the program its create spend revealed."""
    prog = clvm.deserialize(bytes.fromhex(hex_value))
    v = to_list(prog)
    ints = lambda x: to_int(x)  # noqa: E731
    blist = lambda x: [to_bytes(i) for i in to_list(x)] if x.is_pair() else []  # noqa: E731
    if len(v) < 10:  # a short params list (a probe): the split only
        return Params(time_bps=ints(v[0]), fees_bps=ints(v[1]))
    if v[6].is_pair() or v[7].is_pair():
        raise ValueError("gates and boosts are not rebuilt from chain yet")
    collection = None
    if len(v) > 10 and v[10].is_pair():
        pair = to_list(v[10])
        collection = (to_bytes(pair[0]), to_bytes(pair[1]).decode())
    return Params(time_bps=ints(v[0]), fees_bps=ints(v[1]), min_balance=ints(v[2]),
                  blacklist=frozenset(blist(v[3])), whitelist=frozenset(blist(v[4])), forward=bool(ints(v[5])),
                  boost_cap_bps=ints(v[8]), fee_window=ints(v[9]), collection=collection)


@dataclass
class OperatorKey:
    secret: SecretKey  # the synthetic key whose standard puzzle hash is the engine

    @property
    def public_key(self):
        return self.secret.public_key()

    @property
    def puzzle_hash(self) -> bytes:
        return standard_puzzle_hash(self.public_key)


@dataclass
class ServiceState:
    resynced: Resynced
    campaigns: Dict[bytes, Campaign]
    engine: EngineState
    roots: Dict[int, Root] = field(default_factory=dict)
    mismatch: Optional[str] = None  # why proofs stop at some root, if they do
    log: List[dict] = field(default_factory=list)


class CampaignService:
    def __init__(self, chain, launcher_id: bytes, network: str, source_for: Callable[[Sequence[Campaign]], object],
                 operator: Optional[OperatorKey] = None, every: int = 180, puzzles_factory=None):
        self.chain = chain
        self.launcher_id = launcher_id
        self.network = network
        self.source_for = source_for
        self.operator = operator
        self.every = every
        self.puzzles_factory = puzzles_factory or (lambda: Puzzles(Clvm()))
        self.lock = threading.RLock()
        self.s: Optional[ServiceState] = None
        self.signer = KeySigner.for_network([operator.secret] if operator else [], network)

    # --- boot -------------------------------------------------------------------------

    def boot(self) -> ServiceState:
        with self.lock:
            puzzles = self.puzzles_factory()
            got = resync(self.chain.reader, puzzles, self.launcher_id)
            d = got.distributor
            campaigns: Dict[bytes, Campaign] = {}
            for cid, rec in got.campaigns.items():
                c = Campaign(d.launcher_id, bytes.fromhex(rec["creator"]), d.cfg.reward_asset, rec["amount"], rec["start"],
                             rec["end"], Kind(rec["kind"]), bytes.fromhex(rec["target"]),
                             params=params_from_program(puzzles.clvm, rec["params"]), nonce=rec["nonce"],
                             fee_bps=d.cfg.fee_bps, claim_window=rec["claim_window"])
                if c.id.hex() != cid:
                    raise ValueError(f"campaign {cid[:12]} does not rebuild to its id from the chain's fields")
                campaigns[c.id] = c
            start = min((c.start for c in campaigns.values()), default=0)
            state = ServiceState(got, campaigns, EngineState(d.launcher_id, height=start))
            for rid in sorted(got.roots, key=int):
                posted = got.roots[rid]
                covered = [campaigns[bytes.fromhex(k)] for k in posted["totals"] if bytes.fromhex(k) in campaigns]
                try:
                    engine, root = run_epoch(state.engine, covered, self.source_for(covered), posted["end"])
                except Exception as e:  # noqa: BLE001
                    state.mismatch = f"root {rid}: the engine could not rebuild it ({type(e).__name__}: {e})"
                    break
                if root is None or root.root is None or root.root.hex() != posted["root"] or root.root_id != int(rid):
                    state.mismatch = (f"root {rid}: the engine's rebuild does not hash to the root posted on chain; "
                                      "proofs stop at the root before it")
                    break
                state.engine = engine
                state.roots[root.root_id] = root
            self.s = state
            return state

    @property
    def d(self) -> Distributor:
        return self.s.resynced.distributor

    # --- reads --------------------------------------------------------------------------

    def status(self) -> dict:
        with self.lock:
            d, h = self.d, self.chain.peak()
            st = d.chain_state
            camps = []
            for c in st.campaigns:
                full = self.s.campaigns.get(c.id)
                camps.append({
                    "id": c.id.hex(), "creator": c.creator.hex(), "budget": c.budget, "claimed": c.claimed,
                    "start": c.start, "end": c.end, "claim_window": c.claim_window,
                    "kind": full.kind.name if full else None, "target": full.target.hex() if full else None,
                    "excluded": [x.hex() for x in d.excluded.get(c.id, [])],
                    "phase": ("scheduled" if h < c.start else "running" if h < c.end
                              else "claim window" if h < c.end + c.claim_window else "reclaimable"),
                })
            root = lambda r: r and {"root_id": r.root_id, "start": r.start, "end": r.end, "root": r.root.hex(),  # noqa: E731
                                    "activates_at": r.activates_at}
            return {
                "network": self.network, "launcher_id": self.launcher_id.hex(), "peak": h,
                "coin": d.coin.coin_id().hex(), "reserve": d.reserve_coin.amount,
                "reward_asset": d.cfg.reward_asset.hex(), "campaigns": camps,
                "active": root(st.active), "pending": root(st.pending),
                "dispute": st.dispute and {"root_id": st.dispute.root_id, "disputer": st.dispute.disputer_ph.hex()},
                "rotation": st.rotation and {"engine_ph": st.rotation.engine_ph.hex(), "effective_at": st.rotation.effective_at},
                "proofs_through_root": max(self.s.roots, default=0), "proof_gap": self.s.mismatch,
                "operator": self.operator.puzzle_hash.hex() if self.operator else None,
            }

    def claimable(self, puzzle_hash: bytes) -> dict:
        """The holder's leaves in the claimable root: what each pays now, and the message a
        key signature (AGG_SIG_UNSAFE) or a coin's mode-16 message ("k" + it) must carry."""
        with self.lock:
            d = self.d
            st = d.chain_state
            h = self.chain.peak()
            active = st.pending if st.pending and h >= st.pending.activates_at and st.dispute is None else st.active
            if active is None:
                return {"root_id": None, "claims": []}
            root = self.s.roots.get(active.root_id)
            if root is None:
                return {"root_id": active.root_id, "claims": [], "note": self.s.mismatch or "no proofs for this root"}
            out = []
            for cl in root.claims:
                if cl.puzzle_hash != puzzle_hash:
                    continue
                slot = d.claims.get(d.claim_key(cl.campaign_id, puzzle_hash))
                owed = cl.cumulative - (slot.claimed if slot else 0)
                out.append({"campaign": cl.campaign_id.hex(), "cumulative": cl.cumulative, "claimable": max(owed, 0),
                            "leaf": claim_leaf(d.launcher_id, cl.campaign_id, puzzle_hash, cl.cumulative).hex(),
                            "proof": [cl.proof[0], [x.hex() for x in cl.proof[1]]],
                            "excluded": [x.hex() for x in d.excluded.get(cl.campaign_id, [])]})
            return {"root_id": active.root_id, "claims": out}

    # --- the operator -------------------------------------------------------------------

    def due(self, h: int) -> Optional[int]:
        """The end height of the root to post now, or None."""
        d = self.d
        st = d.chain_state
        # A pending root past its activation is promoted by the next spend's prologue, so it does
        # not block posting; one still inside its dispute window (or disputed) does.
        if st.dispute is not None or (st.pending is not None and h < st.pending.activates_at):
            return None
        live = [c for c in st.campaigns if c.id in self.s.campaigns]
        if not live:
            return None
        last_end = self.s.engine.height
        end = min(h, max(c.end for c in live))
        if end <= last_end or h <= min(c.start for c in live):
            return None
        if end - last_end >= self.every or end >= max(c.end for c in live):
            return end
        return None

    def operator_step(self, wait: float = 1800, poll: float = 15) -> Optional[dict]:
        """Post a root if one is due. Returns what was posted, or None."""
        if self.operator is None:
            return None
        with self.lock:
            h = self.chain.peak()
            end = self.due(h)
            if end is None:
                return None
            d = self.d
            if d.engine_at(h) != self.operator.puzzle_hash:
                raise RuntimeError("the service's operator key is not this distributor's engine")
            live = [self.s.campaigns[c.id] for c in d.chain_state.campaigns if c.id in self.s.campaigns]
            engine, root = run_epoch(self.s.engine, live, self.source_for(live), end)
            if root is None or root.root is None:
                return None
            totals = sorted(root.totals.items())
            prepared = d.prepare([d.post_root(h, root.root_id, root.start, root.end, root.root, totals)])
            own = self._message_spends(prepared, d.coin.coin_id())
            spends = list(prepared.spends) + own
            fee = max(MIN_FEE, FEE_PER_COST * d.spend_cost(spends))
            if fee > MIN_FEE:  # rebuild the operator's spend with the fee the bundle's cost needs
                own = self._message_spends(prepared, d.coin.coin_id(), fee)
                spends = list(prepared.spends) + own
            successor = Coin(d.coin.coin_id(), d.full_puzzle(prepared.new_state).tree_hash(), 1)
            self.chain.push(spends, self.signer.sign(own))
            self._wait(successor.coin_id(), wait, poll)
            d.apply(prepared)
            self.s.engine = engine
            self.s.roots[root.root_id] = root
            entry = {"step": "post_root", "root_id": root.root_id, "start": root.start, "end": root.end,
                     "time": int(time.time()), "fee": fee}
            self.s.log.append(entry)
            return entry

    def _message_spends(self, prepared, coin_id: bytes, fee: int = MIN_FEE) -> list:
        """The operator's coin spend carrying every message the leaves need from it, and the fee."""
        clvm = Clvm()
        op = self.operator.puzzle_hash
        coins = sorted((c for c in self.chain.unspent_at([op]) if c.amount >= fee + 1), key=lambda c: c.amount)
        if not coins:
            raise RuntimeError(f"the operator address holds no coin of at least {fee + 1:,} mojos; fund it")
        coin = coins[0]
        conds = []
        for m in prepared.messages:
            if m.sender != op:
                raise RuntimeError("a leaf asks for a message the operator cannot send")
            conds.append([66, m.mode, m.message] + ([] if m.mode == 16 else [coin_id]))
        conds += [[51, op, coin.amount - fee, [op]], [52, fee]]
        from ..chain.values import to_program
        clvm.spend_standard_coin(coin, self.operator.public_key, clvm.delegated_spend([to_program(clvm, c) for c in conds]))
        return list(clvm.coin_spends())

    def _wait(self, coin_id: bytes, wait: float, poll: float) -> int:
        deadline = time.time() + wait
        while True:
            at = self.chain.confirmed_at(coin_id)
            if at:
                return at
            if time.time() > deadline:
                raise RuntimeError(f"not confirmed within {wait:.0f} s")
            time.sleep(poll)
