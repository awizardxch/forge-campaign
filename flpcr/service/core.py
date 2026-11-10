"""The hosted campaign service: one distributor, rebuilt from chain, kept current.

Boot needs no state file:

1. `resync` rebuilds the driver (coin, state, reserve, every slot) from the
   launcher id.
2. The engine reruns every posted root from the same sources the operator used,
   over every campaign live when that root was posted (its posted totals, plus
   every campaign created by then and not yet reclaimed: `root_campaigns`), so a
   root that silently dropped a campaign does not rebuild. Each rebuilt root must
   match the chain (id, window, root, totals, and the manifest hash if one was
   committed), or the service refuses to serve proofs from that root on: a holder
   is never handed a proof the chain would refuse, and the operator posts nothing
   on top of it. A later boot reuses the rebuilt roots (and the engine after each)
   while the chain still names them, and reruns the engine from the first that
   changed.

Then:

* `status()` and `claimable(puzzle_hash)` answer the public API: campaigns,
  roots, and each holder's leaves with the exact message to sign.
* `follow()` notices someone else's spend of the singleton (a dispute, veto,
  rotation, resolve, or a create or post by `scripts/deploy_testnet.py`): one
  coin-state lookup (`chain.spent`) on the coin the driver holds, and a `boot()`
  if it was spent. Every writer (`operator_step`, the sequencer's `run_batch`,
  the intake's `quote`) calls it first, under the service lock and outside a
  lease, so none of them acts on a stale ChainState; the main loop calls it
  each pass so /status follows the chain.
* `operator_step()` posts a root when one is due, signed with the operator key
  the service holds (with the intake's router key, its only keys: the guardian
  and admin stay multisigs). Each root commits `manifest_hash(root)`
  (`flpcr.manifest`), and its manifest is written to `manifests_dir/<hash>.json`
  before the push; `manifest(hash)` serves it (GET /api/v1/manifests/<hash>), so a
  watcher can check every holder's leaf, not only the root and totals.
* the sequencer (`flpcr.service.sequencer`) batches claimants' signed claims.
* the create intake (`flpcr.service.intake`) quotes and co-signs creators' campaigns
  with the router key (a second key, FLPCR_ROUTER_SK). While a quote's lease
  (`flpcr.service.lease`) is live, `operator_step` and the sequencer leave the
  singleton alone.
"""

from __future__ import annotations

import json
import pathlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from chia_wallet_sdk import Clvm, Coin, SecretKey, standard_puzzle_hash

from ..chain.driver import HEIGHT_WINDOW, Distributor, Puzzles
from ..chain.resync import Resynced, resync
from ..chain.signer import KeySigner
from ..chain.values import to_bytes, to_int, to_list
from ..engine import EngineState, Root, run_epoch
from ..manifest import manifest_hash, publish_manifest, root_to_json
from ..merkle import claim_leaf
from ..model import BLOCKS_PER_DAY, Campaign, Kind, Params
from .lease import SingletonLease

MIN_FEE = 2_000_000_000  # 0.002 XCH: the floor the deploy scripts use
FEE_PER_COST = 10  # mojos per unit of cost; under 5 the mempool refuses a nonzero fee
ZERO_HASH = bytes(32).hex()
HASH_HEX = re.compile(r"^[0-9a-f]{64}$")


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


def campaigns_from(got: Resynced, clvm: Clvm) -> Dict[bytes, Campaign]:
    """Every campaign the chain created, rebuilt from its create solution; each must hash to its id."""
    d = got.distributor
    campaigns: Dict[bytes, Campaign] = {}
    for cid, rec in got.campaigns.items():
        c = Campaign(d.launcher_id, bytes.fromhex(rec["creator"]), d.cfg.reward_asset, rec["amount"], rec["start"],
                     rec["end"], Kind(rec["kind"]), bytes.fromhex(rec["target"]),
                     params=params_from_program(clvm, rec["params"]), nonce=rec["nonce"],
                     fee_bps=d.cfg.fee_bps, claim_window=rec["claim_window"])
        if c.id.hex() != cid:
            raise ValueError(f"campaign {cid[:12]} does not rebuild to its id from the chain's fields")
        campaigns[c.id] = c
    return campaigns


def chain_root_ids(got: Resynced) -> List[str]:
    """The posted roots the chain still stands on, in order: through the pending root, or the
    active one. A root vetoed (or revoked by an upheld dispute) and not yet reposted is left
    out, since the next post reuses its id and starts where the active root ended."""
    st = got.distributor.chain_state
    current = max(st.pending.root_id if st.pending else 0, st.active.root_id if st.active else 0)
    return [rid for rid in sorted(got.roots, key=int) if int(rid) <= current]


def reclaim_heights(got: Resynced) -> Dict[bytes, int]:
    """Each reclaimed campaign's id -> the height its reclaim confirmed."""
    out: Dict[bytes, int] = {}
    for a in got.actions:
        if a.leaf == "reclaim":
            out.setdefault(to_bytes(to_list(a.solution)[1]), a.height)
    return out


def root_campaigns(got: Resynced, rid: str, campaigns: Dict[bytes, Campaign],
                   reclaimed: Optional[Dict[bytes, int]] = None) -> List[Campaign]:
    """The campaigns the operator should have passed to the engine for root `rid`: every
    campaign in its posted totals, and every campaign live when it was posted (created by
    then, not reclaimed before it). A campaign live then but missing from the totals still
    accrues in the rebuild, so a root that silently dropped one does not match."""
    posted = got.roots[rid]
    at = posted["posted_at"]
    reclaimed = reclaim_heights(got) if reclaimed is None else reclaimed
    ids = {bytes.fromhex(k) for k in posted["totals"]}
    for cid_hex, rec in got.campaigns.items():
        cid = bytes.fromhex(cid_hex)
        if rec["created_at"] <= at and reclaimed.get(cid, at + 1) > at:
            ids.add(cid)
    return [campaigns[c] for c in sorted(ids) if c in campaigns]


def root_disagrees(root: Optional[Root], rid: str, posted: dict) -> Optional[str]:
    """Why a rebuilt root is not the one posted on chain (the fields that differ), or None."""
    if root is None or root.root is None:
        return "the engine finds nothing claimable in its window"
    mine = {"root_id": root.root_id, "start": root.start, "end": root.end, "root": root.root.hex(),
            "totals": {cid.hex(): amt for cid, amt in sorted(root.totals.items())}}
    theirs = {"root_id": int(rid), "start": posted["start"], "end": posted["end"], "root": posted["root"],
              "totals": posted["totals"]}
    bad = [k for k in mine if mine[k] != theirs[k]]
    mh = posted.get("manifest_hash", ZERO_HASH)
    if mh != ZERO_HASH and mh != manifest_hash(root).hex():
        bad.append("manifest_hash")
    return ", ".join(bad) or None


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
    engines: Dict[int, EngineState] = field(default_factory=dict)  # the engine after each rebuilt root
    posted: Dict[int, Tuple[str, str]] = field(default_factory=dict)  # root id -> (root, manifest hash) as posted
    published: Dict[str, int] = field(default_factory=dict)  # manifest hash -> rebuilt root id


class CampaignService:
    def __init__(self, chain, launcher_id: bytes, network: str, source_for: Callable[[Sequence[Campaign]], object],
                 operator: Optional[OperatorKey] = None, every: int = 180, puzzles_factory=None,
                 manifests_dir: Optional[pathlib.Path] = None, registry=None):
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
        self.lease = SingletonLease()  # held by a create the intake has quoted (flpcr.service.lease)
        self.manifests_dir = pathlib.Path(manifests_dir) if manifests_dir else None  # <hash>.json per posted root
        self.registry = registry  # flpcr.registry.Registry for this network, or None

    # --- boot -------------------------------------------------------------------------

    def boot(self) -> ServiceState:
        with self.lock:
            prior = self.s
            puzzles = self.puzzles_factory()
            got = resync(self.chain.reader, puzzles, self.launcher_id)
            d = got.distributor
            campaigns = campaigns_from(got, puzzles.clvm)
            # No campaign yet: the engine has no start (None, not 0); run_epoch starts it at the
            # earliest campaign's start once there is one.
            start = min((c.start for c in campaigns.values()), default=None)
            state = ServiceState(got, campaigns, EngineState(d.launcher_id, height=start),
                                 log=prior.log if prior else [])
            reclaimed = reclaim_heights(got)
            reuse = prior is not None and prior.resynced.distributor.launcher_id == d.launcher_id
            for rid in chain_root_ids(got):
                posted = got.roots[rid]
                committed = posted.get("manifest_hash", ZERO_HASH)
                state.posted[int(rid)] = (posted["root"], committed)
                old = prior.roots.get(int(rid)) if reuse else None
                if (old is not None and int(rid) in prior.engines and old.root.hex() == posted["root"]
                        and (old.start, old.end) == (posted["start"], posted["end"])):
                    engine, root = prior.engines[int(rid)], old  # unchanged on chain: no rerun
                else:
                    reuse = False  # from here on, rerun the engine
                    covered = root_campaigns(got, rid, campaigns, reclaimed)
                    try:
                        engine, root = run_epoch(state.engine, covered, self.source_for(covered), posted["end"])
                    except Exception as e:  # noqa: BLE001
                        state.mismatch = f"root {rid}: the engine could not rebuild it ({type(e).__name__}: {e})"
                        break
                why = root_disagrees(root, rid, posted)
                if why:
                    state.mismatch = (f"root {rid}: the engine's rebuild does not match the root posted on chain "
                                      f"({why}); proofs stop at the root before it")
                    break
                state.engine = engine
                state.roots[root.root_id] = root
                state.engines[root.root_id] = engine
                self._publish(state, root, committed)
            self.s = state
            return state

    def _publish(self, state: ServiceState, root: Root, committed: str = ZERO_HASH) -> str:
        """Index a rebuilt root's manifest; write its file if the chain committed it and it is not on disk."""
        mh = manifest_hash(root).hex()
        state.published[mh] = root.root_id
        if self.manifests_dir is not None and committed == mh and not (self.manifests_dir / f"{mh}.json").is_file():
            publish_manifest(self.manifests_dir, root)
        return mh

    def follow(self) -> bool:
        """Resync if someone else spent the singleton the driver holds. True if it booted.

        The service's own spends advance the driver as they confirm, so a spent coin here is
        a foreign spend (a dispute, veto, resolve, rotation, a create or post by another
        builder). Skipped while a lease is held: the leased create is the intake's own spend,
        and the intake applies it (or boots) when it settles."""
        with self.lock:
            if self.s is None:
                self.boot()
                return True
            if self.lease.busy() or not self.chain.spent(self.d.coin.coin_id()):
                return False
            before = self.d.coin.coin_id()
            self.boot()
            self.s.log.append({"step": "resync", "reason": "the singleton was spent by another builder",
                               "from_coin": before.hex(), "coin": self.d.coin.coin_id().hex(),
                               "time": int(time.time())})
            return True

    def manifest(self, hash_hex: str) -> Optional[dict]:
        """A posted root's manifest by its hash: the published file if there is one (served as
        written: a watcher checks it against the chain), else the rebuilt root's."""
        hash_hex = hash_hex.strip().lower().removeprefix("0x").removesuffix(".json")
        if not HASH_HEX.match(hash_hex):
            raise ValueError("a manifest hash is 64 hex characters")
        if self.manifests_dir is not None:
            path = self.manifests_dir / f"{hash_hex}.json"
            if path.is_file():
                return json.loads(path.read_text())
        with self.lock:
            rid = self.s.published.get(hash_hex) if self.s else None
            root = self.s.roots.get(rid) if rid is not None else None
            return root_to_json(root) if root is not None else None

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
            def root(r):
                if r is None:
                    return None
                committed = self.s.posted.get(r.root_id)
                mh = committed[1] if committed and committed[0] == r.root.hex() and committed[1] != ZERO_HASH else None
                return {"root_id": r.root_id, "start": r.start, "end": r.end, "root": r.root.hex(),
                        "activates_at": r.activates_at, "manifest_hash": mh}
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

    def distributors(self) -> dict:
        """The registry as loaded at start, and whether this distributor is in it. An index:
        a caller that must trust an entry verifies it against the chain (flpcr.registry.verify)."""
        from ..registry import pins_fingerprint

        reg = self.registry
        mine = reg.get(self.launcher_id.hex()) if reg else None
        return {"network": self.network, "this": self.launcher_id.hex(), "pins": pins_fingerprint(),
                "listed": mine.status if mine else None,
                "distributors": reg.to_json()["distributors"] if reg else []}

    def claimable(self, puzzle_hash: bytes) -> dict:
        """The holder's leaves in the claimable root: what each pays now, and the message a
        key signature (AGG_SIG_UNSAFE) or a coin's mode-16 message ("k" + it) must carry."""
        with self.lock:
            d = self.d
            st = d.chain_state
            h = self.chain.peak()
            active = st.pending if st.pending and h >= st.pending.activates_at and st.dispute is None else st.active
            if active is None:
                return {"root_id": None, "claims": [], "pending": self._pending_for(puzzle_hash, None, None)}
            root = self.s.roots.get(active.root_id)
            if root is None:
                return {"root_id": active.root_id, "claims": [], "pending": [],
                        "note": self.s.mismatch or "no proofs for this root"}
            out = []
            for cl in root.claims:
                if cl.puzzle_hash != puzzle_hash:
                    continue
                slot = d.claims.get(d.claim_key(cl.campaign_id, puzzle_hash))
                claimed = slot.claimed if slot else 0
                owed = cl.cumulative - claimed
                out.append({"campaign": cl.campaign_id.hex(), "cumulative": cl.cumulative, "claimable": max(owed, 0),
                            "claimed": claimed,
                            "leaf": claim_leaf(d.launcher_id, cl.campaign_id, puzzle_hash, cl.cumulative).hex(),
                            "proof": [cl.proof[0], [x.hex() for x in cl.proof[1]]],
                            "excluded": [x.hex() for x in d.excluded.get(cl.campaign_id, [])]})
            return {"root_id": active.root_id, "claims": out, "pending": self._pending_for(puzzle_hash, active, root)}

    def _pending_for(self, puzzle_hash: bytes, effective, effective_root: Optional[Root]) -> list:
        """What the holder has in a posted root that is not claimable yet (in its dispute
        window, or disputed): the amount it adds over the claimable root, never a proof."""
        st = self.d.chain_state
        if st.pending is None or st.pending is effective:
            return []
        root = self.s.roots.get(st.pending.root_id)
        if root is None:
            return []
        out = []
        for cl in root.claims:
            if cl.puzzle_hash != puzzle_hash:
                continue
            before = effective_root.claim_for(cl.campaign_id, puzzle_hash) if effective_root else None
            slot = self.d.claims.get(self.d.claim_key(cl.campaign_id, puzzle_hash))
            base = max(before.cumulative if before else 0, slot.claimed if slot else 0)
            if cl.cumulative > base:
                out.append({"campaign": cl.campaign_id.hex(), "root_id": root.root_id, "cumulative": cl.cumulative,
                            "amount": cl.cumulative - base, "activates_at": st.pending.activates_at,
                            "disputed": st.dispute is not None})
        return out

    def campaign(self, campaign_id: bytes) -> Optional[dict]:
        """One campaign's page: its status row, its terms, what the schedule has released, and
        its total in every root the service rebuilt. None if the chain does not list it."""
        with self.lock:
            st = self.status()
            row = next((x for x in st["campaigns"] if x["id"] == campaign_id.hex()), None)
            if row is None:
                return None
            full = self.s.campaigns.get(campaign_id)
            length = row["end"] - row["start"]
            h = st["peak"]
            out = {**row, "length": length, "daily_emission": row["budget"] * BLOCKS_PER_DAY // length,
                   "reclaim_from": row["end"] + row["claim_window"],
                   "emittable": row["budget"] * (min(max(h, row["start"]), row["end"]) - row["start"]) // length}
            if full is not None:
                out.update({"amount": full.amount, "fee": full.fee, "nonce": full.nonce,
                            "collection": full.params.collection and {"did": full.params.collection[0].hex(),
                                                                      "id": full.params.collection[1]}})
            cs = self.d.chain_state
            active_id = cs.active.root_id if cs.active else None
            pending_id = cs.pending.root_id if cs.pending else None
            roots = []
            for rid, r in sorted(self.s.roots.items()):
                if campaign_id not in r.totals:
                    continue
                state = ("disputed" if rid == pending_id and cs.dispute is not None else "pending" if rid == pending_id
                         else "active" if rid == active_id else "superseded")
                roots.append({"root_id": rid, "start": r.start, "end": r.end, "total": r.totals[campaign_id],
                              "holders": sum(1 for c in r.claims if c.campaign_id == campaign_id and c.cumulative > 0),
                              "state": state})
            out["roots"] = roots
            out["allocated"] = roots[-1]["total"] if roots else 0  # through the last root the service rebuilt
            out["holders"] = roots[-1]["holders"] if roots else 0
            return out

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
        if self.s.mismatch:
            return None  # never post on top of a root the engine cannot rebuild
        last_end = self.s.engine.height if self.s.engine.height is not None else min(c.start for c in live)
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
            if self.lease.busy():  # a quoted create holds the singleton's next generation
                return None
            self.follow()  # someone else may have spent it: a dispute, a veto, a rotation
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
            mh = manifest_hash(root)
            if self.manifests_dir is not None:  # published before the push: the root names it
                publish_manifest(self.manifests_dir, root)
            prepared = d.prepare([d.post_root(h, root.root_id, root.start, root.end, root.root, totals,
                                              manifest_hash=mh)])
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
            self.s.engines[root.root_id] = engine
            self.s.posted[root.root_id] = (root.root.hex(), mh.hex())
            self.s.published[mh.hex()] = root.root_id
            entry = {"step": "post_root", "root_id": root.root_id, "start": root.start, "end": root.end,
                     "time": int(time.time()), "fee": fee, "manifest_hash": mh.hex()}
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
