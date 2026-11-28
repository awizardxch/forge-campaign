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
                 manifests_dir: Optional[pathlib.Path] = None, registry=None, on_demand: bool = False,
                 root_margin: int = 0):
        self.chain = chain
        self.launcher_id = launcher_id
        self.network = network
        self.source_for = source_for
        self.operator = operator
        self.every = every
        # Roots on demand (owner, 2026-11-28: "build roots on demand as a user who clicks claim is
        # actually claiming old confirmed rewards after the dispute window"; "we still want
        # nothing deposited just like merkl does it"). A root goes up when someone asks for one
        # (request_root: a holder's Claim), at most one per `every` blocks, and when a campaign
        # ends (its final root), not on a clock. One root may span any stretch (post_root asks
        # only that it start where the last ended and end by now), so nothing is lost by waiting.
        self.on_demand = on_demand
        self.root_requested = False
        # A root ends this many blocks behind the tip (owner, 2026-11-28: "add the 32 block
        # margin"). Its freshest blocks are the ones a node or an indexer may not have settled yet
        # (a transfer coinset has not indexed, a block reorganized away); left out, they go into
        # the next root, and a campaign's final root goes up `root_margin` blocks after it ends.
        self.root_margin = max(0, int(root_margin))
        self.puzzles_factory = puzzles_factory or (lambda: Puzzles(Clvm()))
        # Two locks (2026-11-10: reads stalled for minutes while a batch waited to confirm):
        # `write_lock` lets one builder at a time (operator, sequencer, create intake, follow,
        # boot) prepare, push and wait on the singleton; `lock` guards only the moments the
        # state is read or swapped. A push's confirmation wait holds `write_lock` alone, so
        # status, claimable and campaign reads answer from the last confirmed state meanwhile.
        self.write_lock = threading.RLock()
        self.lock = threading.RLock()
        self.s: Optional[ServiceState] = None
        self.signer = KeySigner.for_network([operator.secret] if operator else [], network)
        self.lease = SingletonLease()  # held by a create the intake has quoted (flpcr.service.lease)
        self._reclaims = None  # the creator's reclaim desk (flpcr.service.reclaim), made on first use
        self.manifests_dir = pathlib.Path(manifests_dir) if manifests_dir else None  # <hash>.json per posted root
        self.registry = registry  # flpcr.registry.Registry for this network, or None
        self._holder_index: Dict[int, tuple] = {}  # id(root) -> (root, holder -> its leaves)

    @property
    def reclaims(self):
        """The creator's reclaim lane (owner, 2026-11-27): quote, sign, push."""
        if self._reclaims is None:
            from .reclaim import ReclaimDesk
            self._reclaims = ReclaimDesk(self)
        return self._reclaims

    @staticmethod
    def reclaim_ready(st, h: int, campaign_id: bytes):
        """The campaign, if the reclaim leaf would take it at height `h`; else ValueError naming why."""
        camp = st.campaign(campaign_id)
        if camp is None:
            raise ValueError("no such campaign on this distributor (reclaimed already?)")
        opens = camp.end + camp.claim_window
        if h < opens:
            raise ValueError(f"the campaign's claim window is open until height {opens:,}")
        if st.dispute is not None:
            raise ValueError("a dispute is open: a reclaim waits until it is resolved")
        # As the leaf's prologue sees it: a pending root past its activation is the active one.
        active, pending = st.active, st.pending
        if pending is not None and h >= pending.activates_at:
            active, pending = pending, None
        if pending is not None:
            before = dict(active.totals).get(campaign_id, 0) if active is not None else 0
            if dict(pending.totals).get(campaign_id, 0) > before:
                raise ValueError("the pending root owes this campaign's holders more; "
                                 f"reclaim once it activates at height {pending.activates_at:,}")
        return camp

    # --- boot -------------------------------------------------------------------------

    def boot(self) -> ServiceState:
        # The resync and the engine reruns are slow (chain reads): built under `write_lock`,
        # swapped in under `lock`, so reads see the old state until the new one is whole.
        with self.write_lock:
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
            with self.lock:
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
        builder). Skipped while a create is pinned (pushed, waiting to confirm): that is the
        intake's own spend, and the intake applies it (or boots) when it settles. An open quote
        does not stop it: quotes are free, and the state must follow the chain (CS-01)."""
        with self.write_lock:
            if self.s is None:
                self.boot()
                return True
            if self.lease.pinned() or not self.chain.spent(self.d.coin.coin_id()):
                return False
            before = self.d.coin.coin_id()
            held = self.lease.guardian()
            if held is not None and held.coin_id == before:  # the coin a veto named is gone: no hold left
                self.lease.release_guardian()
            self.boot()
            self.s.log.append({"step": "resync", "reason": "the singleton was spent by another builder",
                               "from_coin": before.hex(), "coin": self.d.coin.coin_id().hex(),
                               "time": int(time.time())})
            return True

    # --- the guardian ---------------------------------------------------------------------

    LOCK_ACTIONS = ("veto", "resolve", "rotate", "cancel-rotation", "reclaim")
    # How long a hold lasts for an action with no deadline of its own (resolve, rotate, cancel,
    # reclaim): long enough for a 2-of-4 lock's owners to sign, short enough that claims are not
    # kept waiting for long if they never do.
    LOCK_HOLD_SECONDS = 1800

    def guardian_veto(self, root_id: int, sender_ph: bytes, hold: bool = False) -> dict:
        """The distributor's half of a guardian veto (owner, 2026-11-25); see `lock_action`."""
        return self.lock_action("veto", sender_ph, hold=hold, root_id=root_id)

    def lock_action(self, action: str, sender_ph: bytes, hold: bool = False, *, root_id: Optional[int] = None,
                    upheld: Optional[int] = None, engine_ph: Optional[bytes] = None,
                    campaign_id: Optional[bytes] = None) -> dict:
        """The distributor's half of an action a 🔐 lock takes as its guardian or admin (owner,
        2026-11-25 veto; 2026-11-26 "build the resolve, rotate and reclaim lock proposals"): the
        keyless singleton spend that runs the leaf, built at the chain's height now on the
        singleton's current coin, and the one condition the lock's coin must carry,
        [66, 23, message, coin id]. Nothing is applied here: the spend lands only beside the lock's
        message, and `follow` picks it up after.

          veto             guardian  "v" + tree_hash(root_id)               the pending root, in its window
          resolve          guardian  "s" + tree_hash([root_id, upheld])     the open dispute; the bond coin
                           or admin                                         is spent beside it
          rotate           guardian  "r" + engine_ph                        a new operator key, after the delay
          cancel-rotation  admin     "x" + engine_ph                        the rotation still waiting
          reclaim          admin     "k" + tree_hash(["flpcr-reclaim-v1",   a campaign past its claim
                                             launcher, campaign_id])        window; paid to its creator

        The leaf refuses any other sender; these checks only say why before anyone signs.

        `hold`: also hold the service's own writers (roots, claim batches, creates) off this coin
        while the lock's owners sign (a veto: until the root's dispute window closes; the others:
        LOCK_HOLD_SECONDS), so the signed condition does not go stale."""
        from ..clvm import sha256tree
        from .chain import bundle_json

        if action not in self.LOCK_ACTIONS:
            raise ValueError(f"the lock actions are {', '.join(self.LOCK_ACTIONS)}")
        with self.write_lock:
            self.follow()
            d = self.d
            st = d.chain_state
            guardian, admin = d.cfg.guardian_ph, d.cfg.admin_ph
            h = self.chain.peak()
            if self.lease.pinned():
                raise ValueError("a campaign create is confirming on this coin; retry in a minute")
            out: dict = {}
            seconds = self.LOCK_HOLD_SECONDS
            if action == "veto":
                if sender_ph != guardian:
                    raise ValueError("this address is not the distributor's guardian")
                pending = st.pending
                if pending is None or pending.root_id != root_id:
                    raise ValueError(f"root {root_id} is not this distributor's pending root")
                if h >= pending.activates_at:
                    raise ValueError(f"root {root_id} left its dispute window at height {pending.activates_at:,}; "
                                     "it is active and can no longer be vetoed")
                if st.dispute is not None:
                    raise ValueError(f"root {root_id} is disputed: the guardian settles it with resolve, not veto")
                message = b"v" + sha256tree(root_id)
                call = d.veto(h, root_id, sender_ph)
                seconds = (pending.activates_at - h) * 18.75 + 60
                why = "the guardian is signing a veto of this distributor's pending root"
                out = {"root_id": root_id, "activates_at": pending.activates_at, "blocks_left": pending.activates_at - h}
            elif action == "resolve":
                if sender_ph not in (guardian, admin):
                    raise ValueError("this address is neither the distributor's guardian nor its admin")
                if upheld not in (0, 1) or isinstance(upheld, bool):
                    raise ValueError("upheld is 1 (the disputer was right) or 0 (the root stands)")
                if st.dispute is None:
                    raise ValueError("no dispute is open on this distributor")
                if st.dispute.root_id != root_id:
                    raise ValueError(f"the open dispute is of root {st.dispute.root_id}, not root {root_id}")
                message = b"s" + sha256tree([root_id, upheld])
                call = d.resolve(h, upheld, sender_ph)
                why = "a 🔐 lock is signing the resolution of this distributor's dispute"
                out = {"root_id": root_id, "upheld": upheld, "disputer_ph": st.dispute.disputer_ph.hex(),
                       "bond": d.cfg.dispute_bond,
                       "bond_to": (st.dispute.disputer_ph if upheld else d.cfg.dev_fee_ph).hex()}
            elif action in ("rotate", "cancel-rotation"):
                if not isinstance(engine_ph, (bytes, bytearray)) or len(engine_ph) != 32:
                    raise ValueError("engine_ph is a 32-byte puzzle hash")
                engine_ph = bytes(engine_ph)
                rotation = st.rotation if st.rotation is not None and h < st.rotation.effective_at else None
                if action == "rotate":
                    if sender_ph != guardian:
                        raise ValueError("this address is not the distributor's guardian")
                    if engine_ph == d.engine_at(h):
                        raise ValueError("that key is already the distributor's operator")
                    message = b"r" + engine_ph
                    call = d.rotate(h, engine_ph, sender_ph)
                    why = "the guardian is signing a rotation of this distributor's operator"
                    out = {"engine_ph": engine_ph.hex(), "current_engine_ph": d.engine_at(h).hex(),
                           "rotate_delay": d.cfg.rotate_delay,
                           "replaces": rotation.engine_ph.hex() if rotation else None}
                else:
                    if sender_ph != admin:
                        raise ValueError("this address is not the distributor's admin")
                    if rotation is None:
                        raise ValueError("no rotation is waiting on this distributor")
                    if rotation.engine_ph != engine_ph:
                        raise ValueError(f"the waiting rotation is to {rotation.engine_ph.hex()}, not {engine_ph.hex()}")
                    message = b"x" + engine_ph
                    call = d.cancel_rotation(h, engine_ph, sender_ph)
                    why = "the admin is signing the cancel of this distributor's operator rotation"
                    out = {"engine_ph": engine_ph.hex(), "effective_at": rotation.effective_at,
                           "blocks_left": rotation.effective_at - h}
            else:  # reclaim
                if sender_ph != admin:
                    raise ValueError("this address is not the distributor's admin")
                if not isinstance(campaign_id, (bytes, bytearray)) or len(campaign_id) != 32:
                    raise ValueError("campaign_id is 32 bytes")
                campaign_id = bytes(campaign_id)
                camp = self.reclaim_ready(st, h, campaign_id)
                message = b"k" + sha256tree(["flpcr-reclaim-v1", d.launcher_id, campaign_id])
                from ..chain.auth import ByAdmin
                call = d.reclaim(h, campaign_id, ByAdmin())
                why = "the admin is signing a reclaim on this distributor"
                out = {"campaign_id": campaign_id.hex(), "creator": camp.creator.hex(),
                       "amount": camp.budget - camp.claimed}
            coin_id = d.coin.coin_id()
            mark = d._mark()
            try:
                prepared = d.prepare([call])
                spends = list(prepared.spends)
                cost = d.spend_cost(spends)
            finally:
                d._rewind(mark)
            if hold:
                self.lease.hold_for_guardian(coin_id, seconds, why)
            from chia_wallet_sdk import Signature
            return {
                "action": action, "launcher_id": d.launcher_id.hex(), "coin_id": coin_id.hex(), "height": h,
                **out,
                "sender_ph": sender_ph.hex(), "guardian_ph": sender_ph.hex(), "message": message.hex(),
                "condition": [66, 23, "0x" + message.hex(), "0x" + coin_id.hex()],
                "spends": bundle_json(spends, Signature.infinity())["coin_spends"], "cost": cost,
                "held": hold,
            }

    def release_guardian(self) -> None:
        self.lease.release_guardian()

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
        h = self.chain.peak()  # a node call: never under `lock` (audit 2026-11-10, CS-03)
        with self.lock:
            d = self.d
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
                "roots": "on-demand" if self.on_demand else "scheduled", "root_requested": self.root_requested,
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

    def claimable(self, puzzle_hash: bytes, h: Optional[int] = None) -> dict:
        """The holder's leaves in the claimable root: what each pays now, and the message a
        key signature (AGG_SIG_UNSAFE) or a coin's mode-16 message ("k" + it) must carry.

        `h` is the chain's peak; read here, before the lock, when the caller has not read it.
        The node call is never made under `lock`: one slow answer would stall every read and
        every state swap behind it (audit 2026-11-10, CS-03)."""
        if h is None:
            h = self.chain.peak()
        with self.lock:
            d = self.d
            st = d.chain_state
            active = st.pending if st.pending and h >= st.pending.activates_at and st.dispute is None else st.active
            if active is None:
                return {"root_id": None, "claims": [], "pending": self._pending_for(puzzle_hash, None, None)}
            root = self.s.roots.get(active.root_id)
            if root is None:
                return {"root_id": active.root_id, "claims": [], "pending": [],
                        "note": self.s.mismatch or "no proofs for this root"}
            out = []
            for cl in self._leaves_of(root, puzzle_hash):
                slot = d.claims.get(d.claim_key(cl.campaign_id, puzzle_hash))
                claimed = slot.claimed if slot else 0
                owed = cl.cumulative - claimed
                out.append({"campaign": cl.campaign_id.hex(), "cumulative": cl.cumulative, "claimable": max(owed, 0),
                            "claimed": claimed,
                            "leaf": claim_leaf(d.launcher_id, cl.campaign_id, puzzle_hash, cl.cumulative).hex(),
                            "proof": [cl.proof[0], [x.hex() for x in cl.proof[1]]],
                            "excluded": [x.hex() for x in d.excluded.get(cl.campaign_id, [])]})
            return {"root_id": active.root_id, "claims": out, "pending": self._pending_for(puzzle_hash, active, root)}

    MAX_CLAIMABLE_BATCH = 6000  # a wallet scan's addresses (forge-ui's MAX_PAYER_KEYS)

    def claimable_many(self, puzzle_hashes: List[bytes]) -> dict:
        """`claimable` for a whole wallet in one request (the 🪎 tab asks for every address the
        wallet scan holds): only the addresses with something claimable or pending."""
        if len(puzzle_hashes) > self.MAX_CLAIMABLE_BATCH:
            raise ValueError(f"at most {self.MAX_CLAIMABLE_BATCH} addresses per request")
        holders, root_id = [], None
        h = self.chain.peak()  # once for the whole request, and outside the lock (CS-03)
        with self.lock:
            for ph in dict.fromkeys(puzzle_hashes):  # once each, in order
                one = self.claimable(ph, h)
                root_id = one.get("root_id", root_id)
                if any(c["claimable"] > 0 for c in one["claims"]) or one.get("pending"):
                    holders.append({"puzzle_hash": ph.hex(), **{k: v for k, v in one.items() if k != "root_id"}})
        return {"root_id": root_id, "holders": holders}

    def _leaves_of(self, root: Root, puzzle_hash: bytes) -> list:
        """A root's leaves for one holder, from an index built once per root: a whole-wallet
        lookup is then one dictionary read per address, not a scan of every leaf (CS-03)."""
        index = self._holder_index.get(id(root))
        if index is None or index[0] is not root:
            by_holder: Dict[bytes, list] = {}
            for cl in root.claims:
                by_holder.setdefault(cl.puzzle_hash, []).append(cl)
            if len(self._holder_index) > 8:  # only the claimable and pending roots are ever read
                self._holder_index.clear()
            index = self._holder_index[id(root)] = (root, by_holder)
        return index[1].get(puzzle_hash, [])

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
        for cl in self._leaves_of(root, puzzle_hash):
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
        st = self.status()  # reads the node's peak: before the lock (CS-03)
        with self.lock:
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
        settled = h - self.root_margin  # the newest block a root may cover
        end = min(settled, max(c.end for c in live))
        if end <= last_end or settled <= min(c.start for c in live):
            return None
        if self.on_demand:
            # A campaign that ended since the last root gets its final root; a request waits until
            # the stretch is `every` blocks long, so asking again and again posts no more often.
            if any(last_end < c.end <= settled for c in live):
                return end
            if self.root_requested and end - last_end >= self.every:
                return end
            return None
        if end - last_end >= self.every or end >= max(c.end for c in live):
            return end
        return None

    def request_root(self) -> dict:
        """A holder asks for the rewards since the last root to be posted (on-demand roots). What
        is confirmed already is claimable now; what this root posts is claimable once its dispute
        window closes."""
        h = self.chain.peak()
        with self.lock:
            st = self.d.chain_state
            last_end = self.s.engine.height
            live = [c for c in st.campaigns if c.id in self.s.campaigns]
        if not self.on_demand:
            return {"on_demand": False, "requested": False, "why": "this service posts roots on a schedule"}
        if not live or all(h <= c.start for c in live):
            return {"on_demand": True, "requested": False, "why": "no campaign has accrued anything to post yet"}
        self.root_requested = True
        start = last_end if last_end is not None else min(c.start for c in live)
        posts_at = max(h, start + self.every + self.root_margin)
        return {"on_demand": True, "requested": True, "posts_at": posts_at,
                "claimable_from": posts_at + self.d.cfg.dispute_blocks,
                "dispute_blocks": self.d.cfg.dispute_blocks, "every": self.every}

    def operator_step(self, wait: float = 1800, poll: float = 15) -> Optional[dict]:
        """Post a root if one is due. Returns what was posted, or None."""
        if self.operator is None:
            return None
        with self.write_lock:
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
            successor = d.successor_coin(prepared)
            self.chain.push(spends, self.signer.sign(own))
            self._wait(successor.coin_id(), wait, poll)  # `write_lock` only: reads go on meanwhile
            self.root_requested = False  # this root answers every request made before it
            entry = {"step": "post_root", "root_id": root.root_id, "start": root.start, "end": root.end,
                     "time": int(time.time()), "fee": fee, "manifest_hash": mh.hex()}
            with self.lock:
                d.apply(prepared)
                self.s.engine = engine
                self.s.roots[root.root_id] = root
                self.s.engines[root.root_id] = engine
                self.s.posted[root.root_id] = (root.root.hex(), mh.hex())
                self.s.published[mh.hex()] = root.root_id
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
