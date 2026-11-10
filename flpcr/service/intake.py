"""The create intake: a creator's campaign, quoted, checked and co-signed by our router.

A create needs two parties on chain (CLAUDE.md, FLPCR-07): our router's coin sends
"c" + id and the creator's own coin sends "k" + id, both mode 23 to the singleton's
*current* coin. The creator's spend therefore names one generation, and the intake
holds that generation for it (`flpcr.service.lease`). No puzzle change.

    quote(body)   -> the campaign id, fee, budget, daily emission, reclaim height, the
                     singleton coin to message, the exact conditions the creator's spend
                     must carry, and a lease on the singleton (LEASE_SECONDS).
    create(body)  -> checks the creator's signed bundle (message, deposit, signature,
                     unspent coins), adds the router's spend, the singleton's and a
                     network fee paid by the router's coin, pushes, waits, applies.

Quote request (JSON):

    {"creator": "txch1..." | hex puzzle hash,
     "amount": mojos of the reward asset, the 3% fee included,
     "days": whole days | "length": blocks | "end": height,   (one of them)
     "start": height (optional; default the earliest, quote height + 32),
     "kind": "LP_HOLD" | "NFT_HOLD" | "CAT_HOLD",
     "target": pool launcher id | DID ("did:chia:1..." or hex) | "col1..." | CAT asset id,
     "collection": {"did": "did:chia:1..." | hex, "id": metadata collection id}, (NFT_HOLD by col1)
     "claim_window": blocks (optional; default 7 days, floor 3 x dispute + 32),
     "excluded": [address, ...] (at most 16),
     "reward_asset": hex (optional; must be the distributor's),
     "nonce": int (optional)}

Create request: {"quote": quote id, "spend_bundle": {"coin_spends": [...], "aggregated_signature": hex}}.

`quote` calls `CampaignService.follow()` first, so it names the singleton coin the chain
holds; `create` checks that coin is still unspent (someone else's spend makes the creator's
message stale: 409, re-quote) before the router co-signs.

The router key (FLPCR_ROUTER_SK) is a second key, separate from the operator's. Without
it the intake still quotes, and refuses to create.
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

from chia_wallet_sdk import Address, CatSpend, Clvm, Coin, CoinSpend, PublicKey, SecretKey, Signature, Spend, standard_puzzle_hash

from ..chain.driver import HEIGHT_WINDOW
from ..chain.nfts import collection_id
from ..chain.signer import ADDITIONAL_DATA, AGG_SIG_ME, AGG_SIG_UNSAFE, MAX_COST, KeySigner
from ..chain.values import to_bytes, to_int, to_list, to_program
from ..engine import EngineState
from ..model import BLOCKS_PER_DAY, MAX_EXCLUDED, Campaign, Kind, Params
from ..router import XCH, ForgeRegistry, NotRoutable, RouterRules, check_campaign
from .chain import PushRefused
from .core import FEE_PER_COST, MIN_FEE
from .http import puzzle_hash_of
from .lease import LEASE_SECONDS, LeaseBusy, SingletonLease
from .sequencer import _bundle

MIN_PROTOCOL = 17  # Forge V16 (owner decision 2026-11-09: no V15 or V14 pools)
MODE_CREATE = 23
# The create spend names the quote's height `h`; the leaf accepts it while the block
# that includes it is fewer than HEIGHT_WINDOW blocks later. Leave a margin.
H_MARGIN = 6


class Requote(ValueError):
    """The quote can no longer be honoured: its lease lapsed or the singleton moved."""


@dataclass(frozen=True)
class PoolInfo:
    assets: FrozenSet[bytes]  # bytes(32) for XCH
    protocol: int  # Forge's poolVersion: 17 = V16


def forge_pool_infos(api: str) -> Dict[bytes, PoolInfo]:
    """pool launcher id -> PoolInfo, from a Forge responder's /api/pools."""
    req = urllib.request.Request(f"{api.rstrip('/')}/api/pools", headers={"User-Agent": "flpcr-intake/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    out = {}
    for entry in data.get("pools", []):
        pool = entry.get("pool") or {}
        if not pool.get("launcherCoinId"):
            continue
        assets = frozenset(bytes.fromhex(str(a).removeprefix("0x")) for a in pool.get("assetIds") or [])
        out[bytes.fromhex(pool["launcherCoinId"].removeprefix("0x"))] = PoolInfo(assets, int(pool.get("poolVersion") or 0))
    return out


def cached(fetch: Callable[[], Dict[bytes, PoolInfo]], seconds: float = 300) -> Callable[[], Dict[bytes, PoolInfo]]:
    memo = {"at": 0.0, "value": None}

    def get():
        if memo["value"] is None or time.time() - memo["at"] > seconds:
            memo["value"], memo["at"] = fetch(), time.time()
        return memo["value"]

    return get


@dataclass
class Quote:
    id: str
    campaign: Campaign
    h: int  # the height the create spend names
    coin_id: bytes  # the singleton coin the creator's message names
    expires_at: float
    status: str = "quoted"  # quoted | pushed | created | failed
    detail: Optional[str] = None
    height: Optional[int] = None


def _hex(v) -> bytes:
    return bytes.fromhex(str(v).strip().removeprefix("0x"))


def _id_of(text: str) -> bytes:
    """A 32-byte id: hex, or a bech32m address (did:chia:1..., col1..., xch1...)."""
    text = str(text).strip()
    if text.startswith(("did:chia:", "col1", "xch1", "txch1", "nft1")):
        return Address.decode(text).puzzle_hash
    raw = _hex(text)
    if len(raw) != 32:
        raise ValueError(f"{text[:20]}… is not a 32-byte id")
    return raw


def _int(body: dict, key: str, default=None) -> Optional[int]:
    v = body.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, str)) or not str(v).isdigit():
        raise ValueError(f"{key} must be a non-negative whole number")
    return int(v)


class CampaignIntake:
    def __init__(self, service, pools: Callable[[], Dict[bytes, PoolInfo]], rules: RouterRules = RouterRules(),
                 router_sk: Optional[SecretKey] = None, lease_seconds: float = LEASE_SECONDS):
        self.service = service
        self.pools = pools
        self.rules = rules
        self.router_sk = router_sk
        self.router_ph = standard_puzzle_hash(router_sk.public_key()) if router_sk else None
        self.lease_seconds = lease_seconds
        self.quotes: Dict[str, Quote] = {}

    @property
    def lease(self) -> SingletonLease:
        return self.service.lease

    # --- quote ----------------------------------------------------------------------------

    def campaign_from(self, body: dict, h: int) -> Campaign:
        """The Campaign a quote request describes, checked against the distributor's bounds."""
        d = self.service.d
        cfg = d.cfg
        creator = puzzle_hash_of(str(body.get("creator") or ""))
        if body.get("reward_asset") not in (None, "") and _id_of(body["reward_asset"]) != cfg.reward_asset:
            raise ValueError(f"this distributor pays {'XCH' if cfg.reward_asset == XCH else cfg.reward_asset.hex()}; "
                             "the reward asset is the distributor's")
        amount = _int(body, "amount")
        if not amount:
            raise ValueError("amount (mojos of the reward asset, fee included) is required")
        kind_name = str(body.get("kind") or "").upper()
        if kind_name not in ("LP_HOLD", "NFT_HOLD", "CAT_HOLD"):
            raise ValueError("kind is LP_HOLD, NFT_HOLD or CAT_HOLD")
        kind = Kind[kind_name]
        start = _int(body, "start")
        start = h + cfg.min_start_lead if start is None else start
        if start < h + cfg.min_start_lead:
            raise ValueError(f"start must be at least {h + cfg.min_start_lead} (now {h} + {cfg.min_start_lead})")
        if start > h + cfg.max_campaign_length:
            raise ValueError(f"start must be at most {h + cfg.max_campaign_length}")
        given = [k for k in ("days", "length", "end") if body.get(k) not in (None, "")]
        if len(given) != 1:
            raise ValueError("give exactly one of days, length (blocks) or end (height)")
        if given[0] == "days":
            length = _int(body, "days") * BLOCKS_PER_DAY
        elif given[0] == "length":
            length = _int(body, "length")
        else:
            length = _int(body, "end") - start
        if not cfg.min_campaign_length <= length <= cfg.max_campaign_length:
            raise ValueError(f"length must be {cfg.min_campaign_length} to {cfg.max_campaign_length} blocks")
        window = _int(body, "claim_window")
        window = max(cfg.min_claim_window, 7 * BLOCKS_PER_DAY) if window is None else window
        if not cfg.min_claim_window <= window <= cfg.max_campaign_length:
            raise ValueError(f"claim_window must be {cfg.min_claim_window} to {cfg.max_campaign_length} blocks")
        excluded = body.get("excluded") or []
        if not isinstance(excluded, list) or len(excluded) > MAX_EXCLUDED:
            raise ValueError(f"excluded is a list of at most {MAX_EXCLUDED} addresses")
        blacklist = frozenset(puzzle_hash_of(str(x)) for x in excluded)
        collection = None
        coll = body.get("collection")
        if coll:
            if kind != Kind.NFT_HOLD:
                raise ValueError("only an NFT_HOLD campaign names a collection")
            did, uuid = _id_of(coll.get("did") or ""), str(coll.get("id") or "")
            if not uuid:
                raise ValueError("collection is {did, id}: the minting DID and the metadata's collection id")
            collection = (did, uuid)
            target = collection_id(did, uuid)
            if body.get("target") and _id_of(body["target"]) != target:
                raise ValueError("target is not the col1 id of the collection given")
        else:
            if not body.get("target"):
                raise ValueError("target is required")
            if str(body["target"]).startswith("col1"):
                raise ValueError("a col1 target needs collection {did, id}, so anyone can check membership")
            target = _id_of(body["target"])
        nonce = _int(body, "nonce", 0)
        campaign = None
        for n in range(nonce, nonce + 64):  # the next nonce whose id is free
            campaign = Campaign(d.launcher_id, creator, cfg.reward_asset, amount, start, start + length, kind, target,
                                params=Params(blacklist=blacklist, collection=collection), nonce=n,
                                fee_bps=cfg.fee_bps, claim_window=window)
            if campaign.id not in {x.id for x in d.chain_state.campaigns}:
                break
        return campaign

    def check_route(self, campaign: Campaign) -> None:
        pools = self.pools()
        if campaign.kind == Kind.LP_HOLD:
            info = pools.get(campaign.target)
            if info is not None and info.protocol < MIN_PROTOCOL:
                raise NotRoutable(f"Forge V{info.protocol - 1} pool: campaigns target Forge V16 pools and newer only")
        registry = ForgeRegistry.from_pools({k: v.assets for k, v in pools.items() if v.protocol >= MIN_PROTOCOL})
        st = self.service.d.chain_state
        live = sum(1 for c in st.campaigns if c.creator == campaign.creator)
        check_campaign(campaign, registry, self.rules, live)
        if len(st.campaigns) >= self.service.d.cfg.max_campaigns:
            raise NotRoutable("the distributor is full; try again after a campaign is reclaimed")

    def quote(self, body: dict) -> dict:
        svc = self.service
        if svc.s is None:
            raise ValueError("booting: resyncing from chain")
        if not svc.lock.acquire(timeout=10):
            raise LeaseBusy("the distributor is being spent; retry shortly", 20)
        try:
            svc.follow()  # quote on the singleton as it is on chain, not as this service last moved it
            h = svc.chain.peak()
            campaign = self.campaign_from(body, h)
            try:
                self.check_route(campaign)
            except NotRoutable as e:
                raise ValueError(f"not routable: {e}") from e
            self._prune()
            qid = secrets.token_hex(8)
            coin_id = svc.d.coin.coin_id()
            lease = self.lease.take(qid, coin_id, self.lease_seconds)
            q = self.quotes[qid] = Quote(qid, campaign, h, coin_id, lease.expires_at)
        finally:
            svc.lock.release()
        return self.describe(q)

    def describe(self, q: Quote) -> dict:
        c, d = q.campaign, self.service.d
        message = b"k" + c.id
        asset = "XCH" if c.reward_asset == XCH else c.reward_asset.hex()
        deposit = ({"asset": asset, "amount": c.amount,
                    "how": "the creator's XCH spends leave `amount` mojos unspent beyond their outputs and any "
                           "RESERVE_FEE; the router's spend pays the network fee"}
                   if c.reward_asset == XCH else
                   {"asset": asset, "amount": c.amount,
                    "how": "one or more spends of the creator's coins of this CAT whose CREATE_COINs leave exactly "
                           "`amount` behind; the service joins them to the reserve's CAT ring. The message comes "
                           "from an XCH coin at the creator's address"})
        return {
            "quote": q.id, "campaign": c.id.hex(), "status": q.status,
            "fields": {"distributor": c.distributor_id.hex(), "creator": c.creator.hex(), "reward_asset": c.reward_asset.hex(),
                       "amount": c.amount, "start": c.start, "end": c.end, "kind": c.kind.name, "target": c.target.hex(),
                       "params_hash": c.params.hash().hex(), "excluded": sorted(x.hex() for x in c.params.blacklist),
                       "collection": c.params.collection and {"did": c.params.collection[0].hex(), "id": c.params.collection[1]},
                       "nonce": c.nonce, "fee_bps": c.fee_bps, "claim_window": c.claim_window},
            "fee": c.fee, "budget": c.budget, "length": c.length, "days": c.length / BLOCKS_PER_DAY,
            "daily_emission": c.budget * BLOCKS_PER_DAY // c.length, "reclaim_from": c.reclaim_from,
            "height": q.h, "singleton": {"launcher_id": d.launcher_id.hex(), "coin_id": q.coin_id.hex()},
            "conditions": {
                "message": {"opcode": 66, "mode": MODE_CREATE, "message": message.hex(), "receiver": q.coin_id.hex(),
                            "sender": c.creator.hex(),
                            "condition": [66, MODE_CREATE, "0x" + message.hex(), "0x" + q.coin_id.hex()]},
                "deposit": deposit,
            },
            "lease": {"expires_at": int(q.expires_at), "seconds": int(self.lease_seconds)},
            "router_ready": self.router_sk is not None,
        }

    def _prune(self) -> None:
        cutoff = self.lease.clock() - 3600
        for k in [k for k, q in self.quotes.items() if q.expires_at < cutoff and q.status in ("quoted", "failed")]:
            del self.quotes[k]

    # --- create ---------------------------------------------------------------------------

    def _run(self, clvm: Clvm, puzzle, solution) -> List[list]:
        out = []
        for cond in to_list(puzzle.run(solution, MAX_COST, False).value):
            items = to_list(cond) if cond.is_pair() else []
            if items and items[0].is_atom():
                out.append(items)
        return out

    def check_bundle(self, q: Quote, spends: List[CoinSpend], sig: Signature) -> Tuple[List[CoinSpend], List[CatSpend], int]:
        """(the creator's XCH spends, their CAT deposit spends, mojos the deposit leaves); raises on any fault."""
        svc, c = self.service, q.campaign
        clvm = Clvm()
        if not spends:
            raise ValueError("the spend bundle has no coin spends")
        is_cat = c.reward_asset != XCH
        xch, cats = [], []
        messages, agg = [], []
        inputs = outputs = reserved = 0
        cat_in = cat_out = 0
        for s in spends:
            puzzle, solution = clvm.deserialize(s.puzzle_reveal), clvm.deserialize(s.solution)
            if puzzle.tree_hash() != s.coin.puzzle_hash:
                raise ValueError(f"coin {s.coin.coin_id().hex()[:12]}…: the puzzle reveal is not its puzzle")
            parsed = puzzle.puzzle().parse_cat(s.coin, solution)
            if parsed is not None:
                if not is_cat:
                    raise ValueError("this distributor pays XCH: the deposit is XCH, not a CAT spend")
                if parsed.cat.info.asset_id != c.reward_asset:
                    raise ValueError("a CAT spend of another asset than the reward")
                conds = self._run(clvm, parsed.p2_puzzle.program, parsed.p2_solution)
                dc = svc.d.clvm  # the driver's allocator: the ring is built there
                cats.append(CatSpend(parsed.cat, Spend(dc.deserialize(parsed.p2_puzzle.program.serialize()),
                                                       dc.deserialize(parsed.p2_solution.serialize()))))
                cat_in += s.coin.amount
                for it in conds:
                    if to_int(it[0]) == 51:
                        amt = to_int(it[2])
                        if amt < 0:
                            raise ValueError("a CAT spend may not melt")
                        cat_out += amt
                    elif to_int(it[0]) == 52:
                        raise ValueError("a CAT spend cannot reserve a fee; pay it from XCH")
            else:
                conds = self._run(clvm, puzzle, solution)
                xch.append(s)
                inputs += s.coin.amount
                for it in conds:
                    op = to_int(it[0])
                    if op == 51:
                        outputs += to_int(it[2])
                    elif op == 52:
                        reserved += to_int(it[1])
            for it in conds:
                op = to_int(it[0])
                if op == 66:
                    messages.append((to_int(it[1]), to_bytes(it[2]), to_bytes(it[3]) if len(it) > 3 else None,
                                     s.coin.puzzle_hash if parsed is None else None))
                elif op in (AGG_SIG_ME, AGG_SIG_UNSAFE):
                    msg = to_bytes(it[2])
                    agg.append((to_bytes(it[1]), msg + s.coin.coin_id() + ADDITIONAL_DATA[svc.network] if op == AGG_SIG_ME else msg))
        want = (MODE_CREATE, b"k" + c.id, q.coin_id, c.creator)
        if want not in messages:
            if any(m[1] == b"k" + c.id for m in messages):
                raise ValueError("the creator's message names another coin than the quoted singleton coin")
            raise ValueError("no spend at the creator's address sends \"k\" + campaign id (mode 23) to the quoted singleton coin")
        if len(messages) != 1:
            raise ValueError("the creator's spends must send exactly one message: \"k\" + campaign id")
        if agg:
            if not PublicKey.aggregate_verify([PublicKey.from_bytes(pk) for pk, _ in agg], [m for _, m in agg], sig):
                raise ValueError("the spend bundle's signature does not verify over exactly the signatures its spends need")
        elif not sig.is_infinity():
            raise ValueError("the spend bundle carries a signature its spends do not ask for")
        ids = [s.coin.coin_id() for s in spends]
        if len(set(ids)) != len(ids):
            raise ValueError("a coin is spent twice")
        for cid in ids:
            state = svc.chain.reader.coin_state(cid)
            if state is None or state.spent_height is not None:
                raise ValueError(f"coin {cid.hex()[:12]}… is not an unspent coin on chain")
        if is_cat:
            left = cat_in - cat_out
            if left < c.amount:
                raise ValueError(f"the CAT deposit leaves {left:,}; the campaign needs {c.amount:,}")
            if left > c.amount:
                raise ValueError(f"the CAT deposit leaves {left:,}, more than {c.amount:,}: a CAT ring cannot burn the rest; "
                                 "send it back as change")
            if inputs - outputs - reserved < 0:
                raise ValueError("the creator's XCH spends create more than they spend")
            return xch, cats, left
        left = inputs - outputs - reserved
        if left < c.amount:
            raise ValueError(f"the creator's spends leave {left:,} mojos for the deposit; the campaign needs {c.amount:,}")
        return xch, cats, left

    def _router_spend(self, coin: Coin, coin_id: bytes, cid: bytes, fee: int) -> List[CoinSpend]:
        clvm = Clvm()
        ph = self.router_ph
        conds = [[66, MODE_CREATE, b"c" + cid, coin_id], [51, ph, coin.amount - fee, [ph]], [52, fee]]
        clvm.spend_standard_coin(coin, self.router_sk.public_key(),
                                 clvm.delegated_spend([to_program(clvm, x) for x in conds]))
        return list(clvm.coin_spends())

    def create(self, body: dict, wait: float = 1800, poll: float = 15) -> dict:
        svc = self.service
        if self.router_sk is None:
            raise ValueError("this host does not hold the router key: it quotes, but cannot create")
        if svc.d.cfg.router_ph != self.router_ph:
            raise ValueError("the router key on this host is not this distributor's router")
        q = self.quotes.get(str(body.get("quote") or ""))
        if q is None:
            raise Requote("unknown or expired quote; re-quote")
        if q.status != "quoted":
            raise ValueError(f"this quote is already {q.status}")
        if self.lease.held_by(q.id) is None:
            raise Requote("the quote's lease lapsed; re-quote and sign again")
        spends, sig = _bundle(Clvm(), body.get("spend_bundle"))
        if not svc.lock.acquire(timeout=10):
            raise LeaseBusy("the distributor is being spent; retry shortly", 20)
        try:
            d = svc.d
            if d.coin.coin_id() != q.coin_id or svc.chain.spent(q.coin_id):
                self.lease.release(q.id)
                svc.follow()  # another builder may have spent it: resync before the next quote
                raise Requote("the singleton moved since the quote; re-quote and sign again")
            h = svc.chain.peak()
            if h >= q.h + HEIGHT_WINDOW - H_MARGIN:
                self.lease.release(q.id)
                raise Requote("the quote's height is too old for the create spend; re-quote")
            if q.campaign.id in {x.id for x in d.chain_state.campaigns}:
                raise ValueError("this campaign already exists")
            xch, cats, _ = self.check_bundle(q, spends, sig)
            c = q.campaign
            call = d.create(q.h, c.creator, c.amount, c.start, c.end, int(c.kind), c.target, c.params.as_clvm(),
                            c.nonce, c.claim_window, router=self.router_ph)
            prepared = d.prepare([call], cats)
            mine = {s.coin.coin_id() for s in spends}
            coins = sorted((x for x in svc.chain.unspent_at([self.router_ph]) if x.coin_id() not in mine),
                           key=lambda x: x.amount)
            fee = MIN_FEE
            coin = next((x for x in coins if x.amount >= fee + 1), None)
            if coin is None:
                raise ValueError(f"the router address holds no coin of at least {fee + 1:,} mojos; the host must fund it")
            router = self._router_spend(coin, q.coin_id, c.id, fee)
            all_spends = list(prepared.spends) + xch + router
            need = max(MIN_FEE, FEE_PER_COST * d.spend_cost(all_spends))
            if need > fee:
                coin = next((x for x in coins if x.amount >= need + 1), None)
                if coin is None:
                    raise ValueError(f"the router address holds no coin of at least {need + 1:,} mojos; the host must fund it")
                fee = need
                router = self._router_spend(coin, q.coin_id, c.id, fee)
                all_spends = list(prepared.spends) + xch + router
            signature = Signature.aggregate([sig, KeySigner.for_network([self.router_sk], svc.network).sign(router)])
            successor = Coin(d.coin.coin_id(), d.full_puzzle(prepared.new_state).tree_hash(), 1)
            self.lease.pin(q.id)
            try:
                svc.chain.push(all_spends, signature)
            except PushRefused as e:
                self.lease.release(q.id)
                q.status, q.detail = "failed", str(e)
                raise ValueError(f"the chain refused the create: {e}") from e
            q.status = "pushed"
        finally:
            svc.lock.release()
        try:
            at = svc._wait(successor.coin_id(), wait, poll)
        except RuntimeError as e:
            q.status, q.detail = "failed", f"not confirmed: {e}"
            self.lease.release(q.id)
            with svc.lock:
                svc.boot()  # it may still land; rebuild from chain either way
            raise ValueError(f"the create was pushed but not confirmed: {e}") from e
        with svc.lock:
            d.apply(prepared)
            svc.s.campaigns[c.id] = c
            if svc.s.engine.root_id == 0:  # no root yet: accrual starts where boot would start it
                svc.s.engine = EngineState(d.launcher_id, height=min(x.start for x in svc.s.campaigns.values()))
            svc.s.log.append({"step": "create", "campaign": c.id.hex(), "time": int(time.time()), "fee": fee, "height": at})
            self.lease.release(q.id)
        q.status, q.height = "created", at
        return {"quote": q.id, "campaign": c.id.hex(), "status": q.status, "height": at, "network_fee": fee,
                "budget": c.budget, "fee": c.fee}

    # --- reads ----------------------------------------------------------------------------

    def campaign(self, campaign_id: str) -> Optional[dict]:
        """The service's campaign page (CampaignService.campaign), or a create still in flight."""
        cid = _hex(campaign_id)
        page = self.service.campaign(cid)
        if page is None:
            q = next((q for q in self.quotes.values() if q.campaign.id == cid and q.status == "pushed"), None)
            return q and {"id": cid.hex(), "status": "pushed", "quote": q.id}
        return {**page, "status": "created"}
