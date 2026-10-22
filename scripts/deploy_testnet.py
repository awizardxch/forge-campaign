#!/usr/bin/env python3
"""Drive an FLPCR distributor on testnet11 through the local Sage wallet.

Every bundle is built by `flpcr.chain.driver` (the module the simulator suites
prove), signed by Sage for the wallet coins it spends, pushed to a full node and
confirmed by watching the successor coin appear. Progress lands in
`state-<network>.json` (gitignored) so each step can rebuild the distributor.

    python scripts/deploy_testnet.py launch           # XCH distributor: launcher, eve, init
    python scripts/deploy_testnet.py fund-roles       # coins at the router, operator, creator ... addresses
    python scripts/deploy_testnet.py create --hours 4 --amount 1000000000
    python scripts/deploy_testnet.py post             # a root over the holders' fixture, at the current height
    python scripts/deploy_testnet.py claim            # the holder claims against the active root
    python scripts/deploy_testnet.py reclaim [--admin]
    python scripts/deploy_testnet.py delegate --grant 1 | --grant 0
    python scripts/deploy_testnet.py rotate | cancel-rotation
    python scripts/deploy_testnet.py status

Roles are this wallet's own derivations (a stand-in; the guardian will be a
multisig): router 1, operator 2, guardian 3, creator 4, holder 5, delegate 6.
The dev multisig (dev fee and admin) comes from `flpcr.networks`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from chia_wallet_sdk import Address, Cat, CatSpend, Clvm, Coin, CoinSpend, Constants, Program, PublicKey, Signature, SpendBundle, cat_puzzle_hash, standard_puzzle_hash  # noqa: E402

from flpcr import Campaign, EngineState, FixtureSource, HolderEvent, Kind, run_epoch  # noqa: E402
from flpcr.model import Params  # noqa: E402
from flpcr.live_source import LiveLpSource  # noqa: E402
from flpcr.chain.nfts import ChainNftSource, Combined, collection_id  # noqa: E402
from flpcr.chain.node_reader import NodeReader  # noqa: E402
from flpcr.chain.auth import ByAdmin, ByDelegate, ByMessage, BySignature  # noqa: E402
from flpcr.chain.driver import HEIGHT_WINDOW, ClaimRequest, Distributor, LeafCall, Prepared, Puzzles  # noqa: E402
from flpcr.chain.values import to_program  # noqa: E402
from flpcr.distributor import Config  # noqa: E402
from flpcr.manifest import state_from_json, state_to_json  # noqa: E402
from flpcr.networks import addresses  # noqa: E402

NETWORK = os.environ.get("FLPCR_NETWORK", "testnet11").strip() or "testnet11"
if NETWORK != "testnet11":
    raise SystemExit("this script drives testnet11 only; mainnet needs the owner's own run")
NODE = (os.environ.get("FLPCR_NODE_URL", "").strip() or "https://testnet11.api.coinset.org").rstrip("/")
STATE_FILE = ROOT / (os.environ.get("FLPCR_STATE", "").strip() or f"state-{NETWORK}.json")
# Mojos left as network fee by every bundle, on top of what a leaf reserves. A
# zero-fee spend is accepted by the mempool and never mines.
FEE = int(os.environ.get("FLPCR_FEE", "2000000000"))
ROLES = {"router": 1, "operator": 2, "guardian": 3, "creator": 4, "holder": 5, "delegate": 6, "disputer": 8}
ROLE_FUNDING = 10  # coins per role in `fund-roles`
ROLE_COIN = 3 * FEE  # each role coin covers its own fee a few times over
POOL = b"\xb0" * 32  # the LP_HOLD target: a label on testnet, the router checks it off chain

try:
    sys.path.insert(0, str(ROOT.parent.parent / "discord" / "Forge-Repo" / "projects" / "chia-cfmm" / "contracts"))
    from sage_rpc import SageRPC  # noqa: E402
except ImportError:  # pragma: no cover
    raise SystemExit("sage_rpc.py (Forge-Repo/projects/chia-cfmm/contracts) must be importable")


# ---- node -------------------------------------------------------------------------------------


def rpc(endpoint: str, body: dict, attempts: int = 4) -> dict:
    """One node call, retried on transport failure; push_tx is never retried."""
    req = urllib.request.Request(f"{NODE}/{endpoint}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "flpcr-deploy/1.0"},
                                 method="POST")
    tries = 1 if endpoint == "push_tx" else attempts
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if attempt + 1 >= tries:
                raise
            print(f"  node {endpoint}: {type(exc).__name__}, retrying ({attempt + 2}/{tries})", flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("unreachable")


def peak() -> int:
    """The last transaction block's height, which is what ASSERT_HEIGHT_ABSOLUTE
    is checked against; the raw peak is often a non-transaction block ahead."""
    block = rpc("get_blockchain_state", {})["blockchain_state"]["peak"]
    if block.get("timestamp") is not None:
        return int(block["height"])
    return int(block["prev_transaction_block_height"])


def coin_record(coin_id: bytes) -> Optional[dict]:
    return (rpc("get_coin_record_by_name", {"name": "0x" + coin_id.hex()}) or {}).get("coin_record")


def unspent_at(puzzle_hashes: Sequence[bytes]) -> List[Coin]:
    recs = rpc("get_coin_records_by_puzzle_hashes",
               {"puzzle_hashes": ["0x" + ph.hex() for ph in puzzle_hashes], "include_spent_coins": False})
    out = []
    for r in recs.get("coin_records", []):
        c = r["coin"]
        out.append(Coin(bytes.fromhex(c["parent_coin_info"][2:]), bytes.fromhex(c["puzzle_hash"][2:]), int(c["amount"])))
    return out


# ---- wallet --------------------------------------------------------------------------------------


class Wallet:
    """The local Sage wallet: its keys, its coins (read from the node), and signing."""

    def __init__(self, clvm: Clvm):
        self.clvm = clvm
        self.sage = SageRPC()
        key = self.sage.call("get_key")["key"]
        if key["network_id"] != NETWORK:
            raise SystemExit(f"Sage is on {key['network_id']}, not {NETWORK}")
        self.name = f"{key['name']} ({key['fingerprint']})"
        page = self.sage.call("get_derivations", {"hardened": False, "offset": 0, "limit": 64})["derivations"]
        self.keys: Dict[bytes, PublicKey] = {}
        self.index_of: Dict[bytes, int] = {}
        for d in page:
            pk = PublicKey.from_bytes(bytes.fromhex(d["public_key"]))
            ph = standard_puzzle_hash(pk)
            if ph != Address.decode(d["address"]).puzzle_hash:
                raise SystemExit(f"derivation {d['index']} does not hash to its address")
            self.keys[ph] = pk
            self.index_of[ph] = d["index"]
        self.phs = list(self.keys)
        self.used: set = set()

    def ph(self, role: str) -> bytes:
        return self.phs[ROLES[role]]

    def address(self, ph: bytes) -> str:
        return Address(ph, "txch").encode()

    def pick(self, puzzle_hashes: Sequence[bytes], minimum: int, what: str) -> Coin:
        """The smallest unspent coin at these puzzle hashes holding `minimum`,
        read from the node (Sage's own list lags its spends)."""
        for attempt in range(16):
            coins = sorted((c for c in unspent_at(puzzle_hashes) if c.amount >= minimum and c.coin_id() not in self.used),
                           key=lambda c: c.amount)
            if coins:
                self.used.add(coins[0].coin_id())
                return coins[0]
            print(f"  waiting for an unspent {what} coin >= {minimum:,} (attempt {attempt + 1})", flush=True)
            time.sleep(15)
        raise SystemExit(f"no unspent {what} coin holds {minimum:,}; fund it first")

    def spend(self, coin: Coin, conditions: list) -> CoinSpend:
        """A standard-wallet spend of `coin` emitting `conditions`, unsigned."""
        c = self.clvm
        delegated = c.delegated_spend([to_program(c, cond) for cond in conditions])
        c.spend_standard_coin(coin, self.keys[coin.puzzle_hash], delegated)
        spends = c.coin_spends()
        assert len(spends) == 1
        return spends[0]

    def send(self, sender_ph: bytes, message: bytes, receiver_coin_id: bytes, fee: int = 0,
             extra_outputs: Sequence[tuple] = (), keep: int = 0, mode: int = 23) -> CoinSpend:
        """A coin at `sender_ph` sends `message` to the singleton and recreates
        its value at the same address, less `fee`, `keep` (left in the bundle
        for conservation: a creator's deposit) and any `extra_outputs`."""
        need = fee + keep + sum(a for _, a in extra_outputs)
        coin = self.pick([sender_ph], need + 1, self.address(sender_ph)[:12])
        change = coin.amount - need
        send = [66, mode, message] if mode == 16 else [66, mode, message, receiver_coin_id]
        conditions = [send, [51, sender_ph, change, [sender_ph]]]
        conditions += [[51, ph, amount, [ph]] for ph, amount in extra_outputs]
        if fee:
            conditions.append([52, fee])
        return self.spend(coin, conditions)

    def cat_coin(self, asset_id: bytes, minimum: int, what: str = "reward CAT") -> Cat:
        """The smallest unspent coin of `asset_id` at any of the wallet's addresses holding
        `minimum`, as a spendable Cat: its lineage proof comes from its parent's spend."""
        by_full = {cat_puzzle_hash(asset_id, ph): ph for ph in self.phs}
        for attempt in range(16):
            coins = sorted((c for c in unspent_at(list(by_full)) if c.amount >= minimum and c.coin_id() not in self.used),
                           key=lambda c: c.amount)
            if coins:
                coin = coins[0]
                parent = coin_record(coin.parent_coin_info)
                sol = rpc("get_puzzle_and_solution", {"coin_id": "0x" + coin.parent_coin_info.hex(),
                                                      "height": int(parent["spent_block_index"])})["coin_solution"]
                pc = parent["coin"]
                parent_coin = Coin(bytes.fromhex(pc["parent_coin_info"][2:]), bytes.fromhex(pc["puzzle_hash"][2:]), int(pc["amount"]))
                puzzle = self.clvm.deserialize(bytes.fromhex(sol["puzzle_reveal"][2:])).puzzle()
                children = puzzle.parse_child_cats(parent_coin, self.clvm.deserialize(bytes.fromhex(sol["solution"][2:]))) or []
                match = [x for x in children if x.coin.coin_id() == coin.coin_id()]
                if not match:
                    raise SystemExit(f"could not read the lineage of {what} coin {coin.coin_id().hex()[:12]}")
                self.used.add(coin.coin_id())
                return match[0]
            print(f"  waiting for an unspent {what} coin >= {minimum:,} (attempt {attempt + 1})", flush=True)
            time.sleep(15)
        raise SystemExit(f"no unspent {what} coin of {asset_id.hex()[:12]} holds {minimum:,} in this wallet")

    def cat_spend(self, cat: Cat, conditions: list) -> CatSpend:
        """A standard-wallet spend inside the CAT layer: the inner conditions name inner puzzle hashes."""
        c = self.clvm
        pk = self.keys[cat.info.p2_puzzle_hash]
        return CatSpend(cat, c.standard_spend(pk, c.delegated_spend([to_program(c, cond) for cond in conditions])))

    def fee_spend(self, fee: int) -> CoinSpend:
        ph = self.phs[0]
        coin = self.pick([ph], fee + 1, "fee")
        return self.spend(coin, [[51, ph, coin.amount - fee, [ph]], [52, fee]])

    def sign(self, spends: Sequence[CoinSpend]) -> Signature:
        """Sage signs these spends (only ones its keys can sign are sent)."""
        payload = [{"coin": {"parent_coin_info": s.coin.parent_coin_info.hex(), "puzzle_hash": s.coin.puzzle_hash.hex(),
                             "amount": s.coin.amount},
                    "puzzle_reveal": s.puzzle_reveal.hex(), "solution": s.solution.hex()} for s in spends]
        signed = self.sage.sign_coin_spends(payload)
        bundle = signed.get("spend_bundle")
        if not bundle:
            raise SystemExit(f"Sage did not sign: {signed}")
        return Signature.from_bytes(bytes.fromhex(bundle["aggregated_signature"].removeprefix("0x")))


    def sign_claims(self, signatures: Sequence) -> Signature:
        """The wallet's keys sign claim leaves (AGG_SIG_UNSAFE) in one request:
        a zero-amount stand-in coin whose puzzle is (q . ((49 key leaf) ...)),
        never pushed, so the signature can only authorize those claims. This is
        what a WalletConnect page asks with chip0002_signCoinSpends, the way
        Forge's fee proof does."""
        if not signatures:
            return Signature.infinity()
        conditions = " ".join(f"(49 0x{s.public_key.hex()} 0x{s.message.hex()})" for s in signatures)
        puzzle = self.clvm.parse(f"(q . ({conditions}))")
        stand_in = Coin(hashlib.sha256(b"flpcr-claim-proof").digest(), puzzle.tree_hash(), 0)
        return self.sign([CoinSpend(stand_in, puzzle.serialize(), self.clvm.nil().serialize())])


# ---- bundles -------------------------------------------------------------------------------------


def bundle_json(spends: Sequence[CoinSpend], signature: Signature) -> dict:
    return {
        "coin_spends": [{"coin": {"parent_coin_info": "0x" + s.coin.parent_coin_info.hex(),
                                  "puzzle_hash": "0x" + s.coin.puzzle_hash.hex(), "amount": s.coin.amount},
                         "puzzle_reveal": "0x" + s.puzzle_reveal.hex(), "solution": "0x" + s.solution.hex()}
                        for s in spends],
        "aggregated_signature": "0x" + signature.to_bytes().hex(),
    }


def push_and_wait(wallet: Wallet, puzzle_spends: Sequence[CoinSpend], wallet_spends: Sequence[CoinSpend],
                  watch: bytes, label: str, wait: int = 1800, extra_signature: Optional[Signature] = None) -> dict:
    """Sign the wallet's spends with Sage, push everything, wait for `watch` to exist.
    `extra_signature` (claim leaves the wallet's keys signed) joins the aggregate."""
    signature = wallet.sign(wallet_spends) if wallet_spends else Signature.infinity()
    if extra_signature is not None:
        signature = Signature.aggregate([signature, extra_signature])
    spends = [*puzzle_spends, *wallet_spends]
    state = rpc("get_blockchain_state", {})["blockchain_state"]
    print(f"{label}: {len(spends)} spends, pushing at peak {state['peak']['height']}, "
          f"mempool {state.get('mempool_cost', 0) / 1e9:.1f}B", flush=True)
    verdict = rpc("push_tx", {"spend_bundle": bundle_json(spends, signature)})
    print(f"{label}: push_tx {json.dumps(verdict)}", flush=True)
    if not verdict.get("success"):
        raise SystemExit(f"{label}: rejected by the node")
    deadline = time.time() + wait
    while time.time() < deadline:
        rec = coin_record(watch)
        if rec and int(rec.get("confirmed_block_index") or 0) > 0:
            print(f"{label}: CONFIRMED at height {rec['confirmed_block_index']}", flush=True)
            return {"height": int(rec["confirmed_block_index"]), "status": verdict.get("status")}
        time.sleep(15)
    raise SystemExit(f"{label}: not confirmed within {wait}s; check {watch.hex()} later")


def push_calls(wallet: Wallet, d: Distributor, calls: Sequence[LeafCall], label: str,
               keep_by_sender: Optional[Dict[bytes, int]] = None, fee: int = FEE,
               creator_coin: Optional[Coin] = None, cat_spends: Sequence[CatSpend] = ()) -> Prepared:
    """Prepare, satisfy every message the leaves need from the wallet's role
    coins, add the fee, push, and advance the driver once confirmed.
    `cat_spends`: the wallet's own coins of the reward CAT that join the
    reserve's CAT ring (a creator's deposit); Sage signs their spends."""
    prepared = d.prepare(calls, cat_spends)
    if prepared.unresolved:
        raise SystemExit(f"{label}: local slot records are behind the chain: {[u.hex()[:12] for u in prepared.unresolved]}")
    if any(not s.unsafe for s in prepared.signatures):
        raise SystemExit(f"{label}: a leaf asked for an AGG_SIG_ME signature; this script signs only claim leaves")
    claim_signature = wallet.sign_claims(prepared.signatures)
    ours = {cs.cat.coin.coin_id() for cs in cat_spends}
    if ours:
        signed_cats = wallet.sign([s for s in prepared.spends if s.coin.coin_id() in ours])
        claim_signature = Signature.aggregate([claim_signature, signed_cats])
    wallet_spends = []
    fee_left = fee + prepared.fee
    for i, m in enumerate(prepared.messages):
        keep = (keep_by_sender or {}).get(m.sender, 0)
        this_fee = fee_left if i == 0 else 0
        wallet_spends.append(wallet.send(m.sender, m.message, d.coin.coin_id(), fee=this_fee, keep=keep, mode=m.mode))
        fee_left -= this_fee
    if fee_left:
        wallet_spends.append(wallet.fee_spend(fee_left))
    for ph, amount, memo in prepared.creations:
        # The coin named when the call was built (a dispute's disputer coin) creates it.
        coin = creator_coin
        assert coin is not None, "a creation needs the coin that was named when the call was built"
        wallet_spends.append(wallet.spend(coin, [[51, ph, amount, [memo]], [51, coin.puzzle_hash, coin.amount - amount, [coin.puzzle_hash]]]))
    successor = Coin(d.coin.coin_id(), d.full_puzzle(prepared.new_state).tree_hash(), 1)
    result = push_and_wait(wallet, prepared.spends, wallet_spends, successor.coin_id(), label,
                           extra_signature=claim_signature)
    d.apply(prepared)
    return prepared, result


# ---- state ---------------------------------------------------------------------------------------


def load() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def config_from(state: dict, launcher_id: bytes) -> Config:
    c = state["config"]
    return Config(launcher_id, bytes.fromhex(c["reward_asset"]), bytes.fromhex(c["dev_fee_ph"]),
                  bytes.fromhex(c["admin_ph"]), bytes.fromhex(c["guardian_ph"]), bytes.fromhex(c["router_ph"]),
                  fee_bps=c["fee_bps"], dispute_blocks=c["dispute_blocks"], rotate_delay=c["rotate_delay"],
                  delegate_fee=c["delegate_fee"], min_campaign_length=c["min_campaign_length"],
                  max_campaign_length=c["max_campaign_length"], max_campaigns=c["max_campaigns"],
                  dispute_bond=c["dispute_bond"])


def distributor_from(state: dict, puzzles: Puzzles) -> Distributor:
    if "launcher_id" not in state:
        raise SystemExit("no distributor launched yet: run `launch`")
    launcher_id = bytes.fromhex(state["launcher_id"])
    d = Distributor(puzzles, config_from(state, launcher_id), bytes.fromhex(state["engine_ph"]))
    d.restore(state["driver"])
    # The rebuilt puzzle must be the coin's: a Config field missing from the
    # record curries a different distributor, and the node answers WRONG_PUZZLE_HASH.
    if d.full_puzzle(d.state).tree_hash() != d.coin.puzzle_hash:
        raise SystemExit("the recorded Config does not rebuild the singleton's puzzle; the state file is incomplete")
    return d


def commit(state: dict, d: Distributor, step: str, result: dict, **extra) -> None:
    state["driver"] = d.snapshot()
    state.setdefault("log", []).append({"step": step, "time": int(time.time()), **result, **extra})
    save(state)


def record_campaign(c: Campaign) -> dict:
    return {"id": c.id.hex(), "creator": c.creator.hex(), "amount": c.amount, "start": c.start, "end": c.end,
            "claim_window": c.claim_window, "kind": int(c.kind), "target": c.target.hex(), "nonce": c.nonce,
            "params": [[x.hex() for x in v] if i in (3, 4) else [v[0].hex(), v[1].decode()] if i == 10 else v
                       for i, v in enumerate(c.params.as_clvm())]}


def campaign_records(state: dict) -> dict:
    """id -> record, newest last; an older file with one `campaign` still works."""
    recs = dict(state.get("campaigns") or {})
    if state.get("campaign") and state["campaign"]["id"] not in recs:
        recs[state["campaign"]["id"]] = state["campaign"]
    return recs


def campaign_from(state: dict, d: Distributor, campaign_id: Optional[str] = None) -> Campaign:
    recs = campaign_records(state)
    if not recs:
        raise SystemExit("no campaign recorded yet: run `create`")
    if campaign_id:
        matches = [k for k in recs if k.startswith(campaign_id)]
        if len(matches) != 1:
            raise SystemExit(f"campaign {campaign_id!r} matches {len(matches)} records")
        r = recs[matches[0]]
    else:
        r = list(recs.values())[-1]
    params = Params.from_clvm(r["params"]) if r.get("params") else Params()
    return Campaign(d.launcher_id, bytes.fromhex(r["creator"]), d.cfg.reward_asset, r["amount"], r["start"], r["end"],
                    Kind(r["kind"]), bytes.fromhex(r["target"]), params=params, nonce=r["nonce"], fee_bps=d.cfg.fee_bps,
                    claim_window=r["claim_window"])


FORGE_API = (os.environ.get("FLPCR_FORGE_API", "").strip() or "https://testnet-api.forge.awizard.dev").rstrip("/")
CRAWL_CACHE = ROOT / f"lp-crawl-{NETWORK}.json"


def source_for(args, wallet: "Wallet", campaigns: Sequence[Campaign]):
    """`--source live`: every real LP holder of each target pool, crawled from
    the chain (flpcr.live_source), and every holder of each NFT collection, read
    from its minting DID (flpcr.chain.nfts over a NodeReader); otherwise the
    fixture below."""
    pools = [c.target for c in campaigns if c.kind == Kind.LP_HOLD]
    if getattr(args, "source", "fixture") != "live":
        return holders_source(wallet, pools, [c.target for c in campaigns if c.kind == Kind.NFT_HOLD])
    src = LiveLpSource.connect(FORGE_API, NODE, CRAWL_CACHE)
    src.refresh([src.lp_asset(t) for t in pools])
    for t in pools:
        bal = src.crawls[src.lp_asset(t)].balances()
        print(f"  live source: pool {t.hex()[:12]} LP {src.lp_asset(t).hex()[:12]}: {len(bal)} holders, {sum(bal.values()):,} LP")
    collections = {bytes.fromhex(r["target"]): (bytes.fromhex(r["collection"][0]), r["collection"][1])
                   for r in campaign_records(load()).values() if r.get("collection")}
    nfts = ChainNftSource(NodeReader.connect(NODE), collections=collections)
    for c in campaigns:
        if c.kind == Kind.NFT_HOLD:
            held: Dict[bytes, int] = {}
            for e in nfts.nft_events(c.target, peak() + 1):
                held[e.puzzle_hash] = held.get(e.puzzle_hash, 0) + e.delta
            held = {k: v for k, v in held.items() if v}
            print(f"  live source: collection {c.target.hex()[:12]}: {len(held)} holders, {sum(held.values())} NFTs")
    return Combined(src, nfts)


def holders_source(wallet: Wallet, targets: Sequence[bytes] = (POOL,), collections: Sequence[bytes] = ()) -> FixtureSource:
    """Who holds each target pool's LP: the holder role with 3 units, the
    delegate role with 1, and derivations 10-14 with 2 each (one wallet
    holding LP at several addresses, which `claim-all` claims in one go),
    from height 0. A stand-in for the live Source, which will read the
    pool's real LP holders."""
    lp_assets = {tgt: bytes([0x1b, i]) + bytes(30) for i, tgt in enumerate(targets)}
    spread = [HolderEvent(0, wallet.phs[i], 2) for i in range(10, 15)]
    events = {lp: [HolderEvent(0, wallet.ph("holder"), 3), HolderEvent(0, wallet.ph("delegate"), 1), *spread]
              for lp in lp_assets.values()}
    nfts = {coll: [HolderEvent(0, wallet.ph("holder"), 2), HolderEvent(0, wallet.ph("delegate"), 1)] for coll in collections}
    return FixtureSource(lp_assets=lp_assets, events=events, nfts=nfts)


# ---- commands ----------------------------------------------------------------------------------


def cmd_launch(args, wallet: Wallet, state: dict, puzzles: Puzzles) -> None:
    if "launcher_id" in state and not args.force:
        raise SystemExit(f"already launched: {state['launcher_id']} (use --force to launch another)")
    net = addresses(NETWORK)
    funding = wallet.pick([wallet.phs[0]], FEE + 2, "funding")
    launcher_id = Coin(funding.coin_id(), Constants.singleton_launcher_hash(), 1).coin_id()
    reward_asset = bytes.fromhex(args.reward_asset) if args.reward_asset else bytes(32)
    cfg = Config.testnet(launcher_id, reward_asset, net.dev_fee_ph, net.admin_ph, wallet.ph("guardian"), wallet.ph("router"))
    engine_ph = wallet.ph("operator")
    d = Distributor(puzzles, cfg, engine_ph)
    conditions = d.funding_conditions(funding)
    conditions.append([51, wallet.phs[0], funding.amount - 1 - FEE, [wallet.phs[0]]])
    conditions.append([52, FEE])
    funding_spend = wallet.spend(funding, conditions)
    launcher = d.launcher_spend(funding)
    wallet_spends = [funding_spend]
    if d.is_cat:
        # A coin of the reward CAT creates the empty reserve in its own ring and keeps its value.
        seed = wallet.cat_coin(reward_asset, 1, "seed")
        owner = seed.info.p2_puzzle_hash
        c = wallet.clvm
        c.spend_cats([wallet.cat_spend(seed, [[51, d.reserve_ph, 0, [d.reserve_ph]], [51, owner, seed.coin.amount, [owner]]])])
        wallet_spends += c.coin_spends()
        d.reserve_cat = seed.child(d.reserve_ph, 0)
        d.reserve_coin = d.reserve_cat.coin
    kind = f"CAT {reward_asset.hex()[:12]}" if d.is_cat else "XCH"
    print(f"launching {kind} distributor {launcher_id.hex()} from {funding.coin_id().hex()[:12]}")
    result = push_and_wait(wallet, [launcher], wallet_spends, d.coin.coin_id(), "launch")
    state.clear()
    state.update({
        "network": NETWORK, "launcher_id": launcher_id.hex(), "engine_ph": engine_ph.hex(),
        "wallet": wallet.name,
        "roles": {role: {"index": i, "puzzle_hash": wallet.ph(role).hex(), "address": wallet.address(wallet.ph(role))}
                  for role, i in ROLES.items()},
        "config": {"reward_asset": cfg.reward_asset.hex(), "dev_fee_ph": cfg.dev_fee_ph.hex(),
                   "admin_ph": cfg.admin_ph.hex(), "guardian_ph": cfg.guardian_ph.hex(), "router_ph": cfg.router_ph.hex(),
                   "fee_bps": cfg.fee_bps, "dispute_blocks": cfg.dispute_blocks, "rotate_delay": cfg.rotate_delay,
                   "delegate_fee": cfg.delegate_fee, "dispute_bond": cfg.dispute_bond,
                   "min_campaign_length": cfg.min_campaign_length,
                   "max_campaign_length": cfg.max_campaign_length, "max_campaigns": cfg.max_campaigns,
                   "dev_fee_address": net.dev_fee_address, "admin_address": net.admin_address},
    })
    commit(state, d, "launch", result)
    push_calls(wallet, d, [d.init(peak())], "init")
    commit(state, d, "init", {})
    print(f"distributor live: {launcher_id.hex()}; state in {STATE_FILE.name}")


def cmd_fund_roles(args, wallet: Wallet, state: dict, puzzles: Puzzles) -> None:
    outputs = []
    for role in ROLES:
        ph = wallet.ph(role)
        have = len([c for c in unspent_at([ph]) if c.amount >= ROLE_COIN])
        # Equal outputs to one address from one parent would be one coin id
        # (DUPLICATE_OUTPUT), so each coin differs by a mojo.
        for i in range(max(0, args.count - have)):
            outputs.append((ph, ROLE_COIN + i))
    if not outputs:
        print("every role already holds enough coins"); return
    total = sum(a for _, a in outputs) + FEE
    coin = wallet.pick([wallet.phs[0]], total + 1, "funding")
    conditions = [[51, ph, a, [ph]] for ph, a in outputs]
    conditions += [[51, wallet.phs[0], coin.amount - total, [wallet.phs[0]]], [52, FEE]]
    spend = wallet.spend(coin, conditions)
    first = Coin(coin.coin_id(), outputs[0][0], outputs[0][1])
    push_and_wait(wallet, [], [spend], first.coin_id(), f"fund-roles ({len(outputs)} coins)")


def cmd_create(args, wallet: Wallet, state: dict, puzzles: Puzzles) -> None:
    d = distributor_from(state, puzzles)
    creator = wallet.ph("creator")
    length = int(args.hours * 3600 / 18.75)
    # The creator's own coin supplies the deposit, so it has to hold it first. For a CAT
    # distributor the deposit is a CAT coin and the creator's coin only sends the consent.
    need = (0 if d.is_cat else args.amount) + 2 * FEE
    if not any(x.amount >= need for x in unspent_at([creator])):
        coin = wallet.pick([wallet.phs[0]], need + FEE + 1, "funding")
        spend = wallet.spend(coin, [[51, creator, need, [creator]],
                                    [51, wallet.phs[0], coin.amount - need - FEE, [wallet.phs[0]]], [52, FEE]])
        push_and_wait(wallet, [], [spend], Coin(coin.coin_id(), creator, need).coin_id(), "fund creator")
    # `start` is measured from the height the create names, so both are read
    # after the funding has confirmed.
    h = peak()
    start = h + HEIGHT_WINDOW + args.lead
    excluded = frozenset(Address.decode(a).puzzle_hash if a.startswith(("txch1", "xch1")) else bytes.fromhex(a)
                         for a in (args.exclude or []))
    kind = Kind.NFT_HOLD if args.kind == "nft" else Kind.LP_HOLD
    collection = None
    if kind == Kind.NFT_HOLD and args.collection_id:
        # A collection as wallets show it: the minting DID + the metadata's collection id, named col1...
        did = Address.decode(args.collection_did).puzzle_hash
        target = collection_id(did, args.collection_id)
        collection = [did.hex(), args.collection_id]
        print(f"  collection {Address(target, 'col').encode()} = DID {did.hex()[:12]} + {args.collection_id}")
    else:
        target = Address.decode(args.target).puzzle_hash if args.target.startswith(("did:", "col1")) else bytes.fromhex(args.target)
    # A collection campaign commits its (DID, collection id) in params, so the chain alone tells
    # anyone how to check membership.
    params = Params(blacklist=excluded, collection=(bytes.fromhex(collection[0]), collection[1]) if collection else None)
    c = Campaign(d.launcher_id, creator, d.cfg.reward_asset, args.amount, start, start + length,
                 kind, target, params=params, nonce=args.nonce,
                 fee_bps=d.cfg.fee_bps, claim_window=max(d.cfg.min_claim_window, args.window))
    if excluded:
        print(f"  excluded from this campaign: {[wallet.address(x) if x in wallet.keys else x.hex()[:16] for x in excluded]}")
    print(f"campaign {c.id.hex()[:12]}: {c.amount:,} mojos, blocks {c.start}-{c.end} ({length} blocks, "
          f"~{length * 18.75 / 3600:.1f} h), window {c.claim_window}, fee {c.fee:,}, budget {c.budget:,}")
    call = d.create(h, c.creator, c.amount, c.start, c.end, int(c.kind), c.target, c.params.as_clvm(), c.nonce,
                    c.claim_window)
    if d.is_cat:
        deposit = wallet.cat_coin(d.cfg.reward_asset, c.amount, "deposit")
        owner = deposit.info.p2_puzzle_hash
        change = deposit.coin.amount - c.amount
        cat_spend = wallet.cat_spend(deposit, [[51, owner, change, [owner]]] if change else [])
        _, result = push_calls(wallet, d, [call], "create", cat_spends=[cat_spend])
    else:
        _, result = push_calls(wallet, d, [call], "create", keep_by_sender={c.creator: c.amount})
    state.setdefault("campaigns", {})[c.id.hex()] = {**record_campaign(c), **({"collection": collection} if collection else {})}
    state["campaign"] = record_campaign(c)
    commit(state, d, "create", result, campaign_id=c.id.hex())


def cmd_post(args, wallet: Wallet, state: dict, puzzles: Puzzles) -> None:
    d = distributor_from(state, puzzles)
    live = [campaign_from(state, d, k.hex()) for k in d.chain_state.by_id if k.hex() in campaign_records(state)]
    if not live:
        raise SystemExit("no live campaign")
    h = peak()
    end = min(h, max(c.end for c in live)) if args.end is None else args.end
    engine = state_from_json(state["engine"]) if state.get("engine") else EngineState(d.launcher_id, height=min(c.start for c in live))
    engine, root = run_epoch(engine, live, source_for(args, wallet, live), end)
    if root is None or root.root is None:
        raise SystemExit("nothing accrued in this window; wait for the campaign to start")
    totals = sorted(root.totals.items())
    call = d.post_root(h, root.root_id, root.start, root.end, root.root, totals)
    _, result = push_calls(wallet, d, [call], f"post root {root.root_id} [{root.start}, {root.end})")
    engine_before = state.get("engine")
    state["engine"] = state_to_json(engine)
    state.setdefault("roots", {})[str(root.root_id)] = {
        "root": root.root.hex(), "start": root.start, "end": root.end, "totals": {k.hex(): v for k, v in totals},
        "engine_before": engine_before,
        "claims": [{"campaign": cl.campaign_id.hex(), "puzzle_hash": cl.puzzle_hash.hex(), "cumulative": cl.cumulative,
                    "proof": [cl.proof[0], [x.hex() for x in cl.proof[1]]]} for cl in root.claims],
        "activates_at": h + HEIGHT_WINDOW + d.cfg.dispute_blocks,
    }
    commit(state, d, "post", result, root_id=root.root_id)


def active_root(state: dict, d: Distributor) -> dict:
    st = d.chain_state
    h = peak()
    pending = st.pending
    root = st.active
    if pending is not None and h >= pending.activates_at:
        root = pending
    if root is None:
        raise SystemExit("no active root yet")
    return state["roots"][str(root.root_id)]


def cmd_claim(args, wallet: Wallet, state: dict, puzzles: Puzzles) -> None:
    d = distributor_from(state, puzzles)
    c = campaign_from(state, d, args.campaign)
    root = active_root(state, d)
    holder = wallet.ph(args.holder)
    cl = next((x for x in root["claims"] if x["puzzle_hash"] == holder.hex() and x["campaign"] == c.id.hex()), None)
    if cl is None:
        raise SystemExit(f"{args.holder} has no leaf in the active root")
    proof = (cl["proof"][0], [bytes.fromhex(x) for x in cl["proof"][1]])
    auth = (ByDelegate(wallet.ph("delegate")) if args.delegated
            else BySignature(wallet.keys[holder].to_bytes()) if args.by_key else ByMessage())
    call = d.claim(peak(), c.id, holder, cl["cumulative"], proof, auth)
    before = sum(x.amount for x in unspent_at([holder]))
    _, result = push_calls(wallet, d, [call], f"claim {args.holder} cumulative {cl['cumulative']:,}")
    commit(state, d, "claim", result, holder=args.holder, cumulative=cl["cumulative"], campaign_id=c.id.hex())
    time.sleep(20)
    print(f"{args.holder} balance {before:,} -> {sum(x.amount for x in unspent_at([holder])):,}")


def cmd_claim_all(args, wallet: Wallet, state: dict, puzzles: Puzzles) -> None:
    """Every address of this wallet that has a leaf in the active root claims,
    by key signature, through the sequencer: one wallet request signs them
    all, one coin spend pays the fee, one bundle (chained generations) pays
    them. What a WalletConnect claim does."""
    d = distributor_from(state, puzzles)
    root = active_root(state, d)
    campaigns = {k for k in campaign_records(state)} if not args.campaign else {
        k for k in campaign_records(state) if k.startswith(args.campaign)}
    requests = []
    for cl in root["claims"]:
        ph = bytes.fromhex(cl["puzzle_hash"])
        if ph not in wallet.keys or cl["campaign"] not in campaigns:
            continue
        cid = bytes.fromhex(cl["campaign"])
        rec = d.claims.get(d.claim_key(cid, ph))
        if rec is not None and rec.claimed >= cl["cumulative"]:
            continue
        proof = (cl["proof"][0], [bytes.fromhex(x) for x in cl["proof"][1]])
        requests.append(ClaimRequest(cid, ph, cl["cumulative"], proof, BySignature(wallet.keys[ph].to_bytes())))
    if not requests:
        raise SystemExit("nothing to claim for this wallet in the active root")
    h = peak()
    batch = d.sequence(h, requests)
    if batch.rejected:
        raise SystemExit(f"the leaf refused: {[(r.puzzle_hash.hex()[:12], why[:60]) for r, why in batch.rejected]}")
    if batch.unresolved:
        raise SystemExit("local slot records are behind the chain; resync first")
    label = f"claim-all {len(batch.placed)} addresses in {len(batch.generations)} generations"
    before = {r.puzzle_hash: sum(x.amount for x in unspent_at([r.puzzle_hash])) for r in batch.placed}
    signature = wallet.sign_claims(batch.signatures)
    last = batch.generations[-1]
    successor = Coin(last.spends[0].coin.coin_id(), d.full_puzzle(last.new_state).tree_hash(), 1)
    # A nonzero fee under 5 mojos per unit of cost is refused (INVALID_FEE_TOO_CLOSE_TO_ZERO), and a
    # bundle of several claims costs far more than one action: size the fee from the bundle. The
    # signatures (~1.2M each) and the fee coin's own spend (~30M) sit outside batch.cost.
    cost = batch.cost + 1_200_000 * len(batch.signatures) + 30_000_000
    fee = max(FEE, 10 * cost)
    print(f"{label}: cost ~{cost:,}, fee {fee:,} mojos ({fee / cost:.1f}/cost)")
    result = push_and_wait(wallet, batch.spends, [wallet.fee_spend(fee)], successor.coin_id(), label,
                           extra_signature=signature)
    d.apply_batch(batch)
    commit(state, d, "claim-all", result, holders=[wallet.address(r.puzzle_hash) for r in batch.placed],
           cumulative=sum(r.cumulative for r in batch.placed), deferred=len(batch.deferred))
    time.sleep(20)
    for r in batch.placed:
        after = sum(x.amount for x in unspent_at([r.puzzle_hash]))
        print(f"  {wallet.address(r.puzzle_hash)[:20]}…  {before[r.puzzle_hash]:,} -> {after:,}")
    if batch.deferred:
        print(f"{len(batch.deferred)} claims did not fit; run claim-all again")


def reclaim_blockers(state: dict, d: Distributor, c: Campaign, h: int) -> List[str]:
    """Why reclaiming `c` now would take rewards its holders never had a fair
    chance to claim. The puzzle allows a reclaim from end + claim_window, and by
    design what nobody claims goes back to the creator; these checks keep an
    operator (or a creator's button) from reclaiming in the gap between a root
    being posted and its holders being able to claim it:
      * the campaign's emission is not fully posted (the last root ends before the campaign does);
      * a root carrying this campaign is still pending (posted, not yet claimable);
      * the root that completed it became claimable less than claim_window blocks ago,
        and holders still have unclaimed amounts in it.
    """
    st = d.chain_state
    cid = c.id.hex()
    reasons = []
    covering = [(int(k), r) for k, r in state.get("roots", {}).items() if cid in r.get("totals", {})]
    last_end = max((r["end"] for _, r in covering), default=c.start)
    if last_end < c.end:
        reasons.append(f"its emission is posted only up to block {last_end:,} of {c.end:,}: post the final root first")
    if st.pending is not None and dict((k.hex(), v) for k, v in st.pending.totals).get(cid, 0):
        reasons.append(f"root {st.pending.root_id} is still pending (claimable from block {st.pending.activates_at:,})")
    final = next((r for _, r in sorted(covering, key=lambda kr: kr[0]) if r["end"] >= c.end), None)
    if final is not None:
        owed = 0
        for cl in final["claims"]:
            if cl["campaign"] != cid:
                continue
            rec = d.claims.get(d.claim_key(c.id, bytes.fromhex(cl["puzzle_hash"])))
            owed += max(0, cl["cumulative"] - (rec.claimed if rec else 0))
        open_for = h - final["activates_at"]
        if owed and open_for < c.claim_window:
            reasons.append(f"holders still have {owed:,} mojos unclaimed in the final root, which has been claimable "
                           f"for {max(open_for, 0)} of the {c.claim_window} blocks a claim window gives them")
    return reasons


def cmd_reclaim(args, wallet: Wallet, state: dict, puzzles: Puzzles) -> None:
    d = distributor_from(state, puzzles)
    c = campaign_from(state, d, args.campaign)
    h = peak()
    blockers = reclaim_blockers(state, d, c, h)
    if blockers and not args.force:
        raise SystemExit(f"not reclaiming campaign {c.id.hex()[:12]}: " + "; ".join(blockers)
                         + " (--force reclaims anyway)")
    call = d.reclaim(h, c.id, ByAdmin() if args.admin else ByMessage())
    _, result = push_calls(wallet, d, [call], "reclaim" + (" (admin)" if args.admin else ""))
    commit(state, d, "reclaim", result, admin=args.admin, campaign_id=c.id.hex())


def cmd_delegate(args, wallet: Wallet, state: dict, puzzles: Puzzles) -> None:
    d = distributor_from(state, puzzles)
    owner, delegate = wallet.ph(args.owner), wallet.ph("delegate")
    call = d.delegate(peak(), owner, delegate, args.grant, ByMessage())
    _, result = push_calls(wallet, d, [call], f"delegate grant={args.grant} for {args.owner}")
    commit(state, d, "delegate", result, owner=args.owner, grant=args.grant)


def cmd_dispute(args, wallet: Wallet, state: dict, puzzles: Puzzles) -> None:
    """Anyone contests the pending root with a bond; here the disputer role."""
    d = distributor_from(state, puzzles)
    pending = d.chain_state.pending
    if pending is None:
        raise SystemExit("no root pending")
    ph = wallet.ph("disputer")
    bond = d.cfg.dispute_bond
    # The disputer's own coin creates the bond launcher, so it must hold the bond first.
    if not any(x.amount >= bond + FEE + 1 for x in unspent_at([ph])):
        src = wallet.pick([wallet.phs[0]], bond + 3 * FEE, "funding")
        spend = wallet.spend(src, [[51, ph, bond + FEE + 1, [ph]], [51, wallet.phs[0], src.amount - bond - 2 * FEE - 1, [wallet.phs[0]]], [52, FEE]])
        push_and_wait(wallet, [], [spend], Coin(src.coin_id(), ph, bond + FEE + 1).coin_id(), "fund disputer")
    coin = wallet.pick([ph], bond + 1, "disputer")
    call = d.dispute(peak(), pending.root_id, ph, coin.coin_id())
    _, result = push_calls(wallet, d, [call], f"dispute root {pending.root_id} with a {bond:,} mojo bond",
                           creator_coin=coin)
    commit(state, d, "dispute", result, root_id=pending.root_id)


def revoke_root(state: dict, root_id: int, why: str) -> None:
    """The chain dropped a pending root (veto, or an upheld dispute): the next
    post carries the same root id, so the engine rewinds to the snapshot that
    root was built from, and the record moves aside so no claim is cut from it."""
    rec = state.get("roots", {}).pop(str(root_id), None)
    if rec is None:
        return
    state["engine"] = rec.get("engine_before")
    if state["engine"] is None:
        state.pop("engine", None)
    state.setdefault("revoked_roots", []).append({"root_id": root_id, "why": why, **{k: v for k, v in rec.items() if k != "engine_before"}})


def cmd_resolve(args, wallet: Wallet, state: dict, puzzles: Puzzles) -> None:
    d = distributor_from(state, puzzles)
    dispute = d.chain_state.dispute
    if dispute is None:
        raise SystemExit("no dispute open")
    sender = wallet.ph("guardian") if args.by == "guardian" else d.cfg.admin_ph
    call = d.resolve(peak(), 1 if args.upheld else 0, sender=sender)
    label = f"resolve dispute: {'upheld, refund' if args.upheld else 'rejected, forfeit'}"
    prepared = d.prepare([call])
    wallet_spends = [wallet.send(m.sender, m.message, d.coin.coin_id(), fee=FEE) for m in prepared.messages]
    successor = Coin(d.coin.coin_id(), d.full_puzzle(prepared.new_state).tree_hash(), 1)
    result = push_and_wait(wallet, prepared.spends, wallet_spends, successor.coin_id(), label)
    d.apply(prepared)
    if args.upheld:
        revoke_root(state, dispute.root_id, "dispute upheld")
    elif d.chain_state.pending is not None:  # rejected: the claim window restarts from the resolution
        state["roots"][str(dispute.root_id)]["activates_at"] = d.chain_state.pending.activates_at
    commit(state, d, "resolve", result, upheld=bool(args.upheld), root_id=dispute.root_id)


def cmd_rotate(args, wallet: Wallet, state: dict, puzzles: Puzzles) -> None:
    d = distributor_from(state, puzzles)
    new = wallet.phs[7]
    call = d.rotate(peak(), new)
    _, result = push_calls(wallet, d, [call], f"rotate -> derivation 7")
    commit(state, d, "rotate", result, new_engine_ph=new.hex())


def cmd_cancel_rotation(args, wallet: Wallet, state: dict, puzzles: Puzzles) -> None:
    d = distributor_from(state, puzzles)
    rotation = d.chain_state.rotation
    if rotation is None:
        raise SystemExit("no rotation pending")
    call = d.cancel_rotation(peak(), rotation.engine_ph)
    _, result = push_calls(wallet, d, [call], "cancel rotation")
    commit(state, d, "cancel-rotation", result)


def cmd_resync(args, wallet, state: dict, puzzles: Puzzles) -> None:
    """Rebuild the record's driver state from the chain (flpcr.chain.resync) and compare it with the
    record; --write saves it. Campaign fields and roots the record lacks are filled in from the
    chain; root proofs and the wallet's roles are not on chain, so the record keeps its own."""
    from flpcr.chain.resync import ResyncError, resync
    launcher = args.launcher or state.get("launcher_id")
    if not launcher:
        raise SystemExit("no launcher id: pass --launcher or keep one in the record")
    try:
        got = resync(NodeReader.connect(NODE), puzzles, bytes.fromhex(launcher.removeprefix("0x")))
    except ResyncError as e:
        raise SystemExit(f"resync refused: {e}")
    d, cfg = got.distributor, got.config
    print(f"distributor {launcher[:12]}: {got.generations} generations, {len(got.actions)} actions replayed; "
          f"coin {d.coin.coin_id().hex()[:12]} unspent, reserve {d.reserve_coin.amount:,}")
    print(f"  {len(got.campaigns)} campaign(s) and {len(got.roots)} root(s) on chain; "
          f"{len(d.claims) - 2} claim slot(s), {len(d.delegations) - 2} delegation slot(s)")
    snap = d.snapshot()
    if state.get("driver"):
        diff = [k for k in snap if snap[k] != state["driver"].get(k)]
        print("  the record matches the chain" if not diff else f"  the record differs from the chain in: {', '.join(diff)}")
    else:
        print("  the record has no driver state yet")
    if not args.write:
        print("  (compare only; --write saves the chain's state into the record)")
        return
    state.setdefault("network", NETWORK)
    state["launcher_id"] = launcher.removeprefix("0x")
    state["engine_ph"] = state.get("engine_ph") or d.chain_state.engine_ph.hex()
    config = state.setdefault("config", {})
    for name in ("reward_asset", "dev_fee_ph", "admin_ph", "guardian_ph", "router_ph"):
        config[name] = getattr(cfg, name).hex()
    for name in ("fee_bps", "dispute_blocks", "rotate_delay", "delegate_fee", "min_campaign_length",
                 "max_campaign_length", "max_campaigns", "dispute_bond"):
        config[name] = getattr(cfg, name)
    state["driver"] = snap
    records = state.setdefault("campaigns", {})
    for cid, fields in got.campaigns.items():
        if cid not in records:
            records[cid] = {**fields, "from_chain": True}
    roots = state.setdefault("roots", {})
    for rid, root in got.roots.items():
        if rid not in roots:
            roots[rid] = {**root, "claims": [], "from_chain": True}
    commit(state, d, "resync", {"generations": got.generations}, launcher_id=state["launcher_id"])
    print(f"  saved to {STATE_FILE.name}")


def cmd_status(args, wallet: Wallet, state: dict, puzzles: Puzzles) -> None:
    d = distributor_from(state, puzzles)
    st = d.chain_state
    rec = coin_record(d.coin.coin_id())
    print(f"peak {peak()}  singleton {d.coin.coin_id().hex()[:16]} "
          f"{'unspent' if rec and not rec['spent'] else 'SPENT or unknown: resync needed'}")
    print(f"reserve {st.reserve:,} (chain {coin_record(d.reserve_coin.coin_id())['coin']['amount']:,})  "
          f"engine {wallet.index_of.get(st.engine_ph, st.engine_ph.hex()[:12])}  rotation {st.rotation}")
    for c in st.campaigns:
        print(f"campaign {c.id.hex()[:12]} creator {wallet.index_of.get(c.creator, '?')} budget {c.budget:,} "
              f"claimed {c.claimed:,} blocks {c.start}-{c.end} window {c.claim_window}")
    print(f"active {st.active and st.active.root_id}  pending {st.pending and (st.pending.root_id, st.pending.activates_at)}"
          f"  dispute {st.dispute and (st.dispute.root_id, wallet.index_of.get(st.dispute.disputer_ph, '?'))}")
    print(f"claim slots {len(d.claims)}  delegation slots {len(d.delegations)}")
    for role in ROLES:
        ph = wallet.ph(role)
        print(f"  {role:9s} {wallet.address(ph)}  {sum(c.amount for c in unspent_at([ph])):,}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("launch"); s.add_argument("--force", action="store_true")
    s.add_argument("--reward-asset", help="pay rewards in this CAT (its asset id); default XCH")
    s = sub.add_parser("fund-roles"); s.add_argument("--count", type=int, default=ROLE_FUNDING)
    s = sub.add_parser("create")
    s.add_argument("--hours", type=float, default=4.0)
    s.add_argument("--amount", type=int, default=1_000_000_000)
    s.add_argument("--window", type=int, default=0, help="claim window in blocks (floor: the Config minimum)")
    s.add_argument("--lead", type=int, default=8, help="blocks after h + 32 the campaign starts")
    s.add_argument("--exclude", action="append", help="an address (or puzzle hash) this campaign never pays; repeatable, at most 16")
    s.add_argument("--nonce", type=int, default=0)
    s.add_argument("--target", default=POOL.hex(),
                   help="LP: the pool launcher id; NFT: the minting DID (did:chia:... or its launcher id hex)")
    s.add_argument("--kind", choices=("lp", "nft"), default="lp", help="what the campaign rewards: LP holders or an NFT collection")
    s.add_argument("--collection-did", help="NFT: the minting DID (did:chia:...)")
    s.add_argument("--collection-id", help="NFT: the collection id in the metadata (target becomes its col1 id)")
    s = sub.add_parser("post"); s.add_argument("--end", type=int)
    s.add_argument("--source", choices=("fixture", "live"), default="fixture", help="live: every real LP holder, crawled from the chain")
    s = sub.add_parser("claim"); s.add_argument("--holder", default="holder"); s.add_argument("--delegated", action="store_true")
    s.add_argument("--by-key", action="store_true", help="the holder's key signs the leaf; no coin at the holder needed")
    s.add_argument("--campaign", help="campaign id prefix; default the newest")
    s = sub.add_parser("claim-all", help="every address of this wallet in the active root, one bundle")
    s.add_argument("--campaign", help="campaign id prefix; default every campaign")
    s = sub.add_parser("reclaim"); s.add_argument("--admin", action="store_true")
    s.add_argument("--force", action="store_true", help="reclaim even while holders still have rewards they could not yet claim")
    s.add_argument("--campaign", help="campaign id prefix; default the newest")
    s = sub.add_parser("delegate"); s.add_argument("--grant", type=int, default=1); s.add_argument("--owner", default="holder")
    sub.add_parser("dispute")
    s = sub.add_parser("resolve"); s.add_argument("--upheld", type=int, default=1, choices=(0, 1))
    s.add_argument("--by", default="guardian", choices=("guardian",))
    sub.add_parser("rotate")
    sub.add_parser("cancel-rotation")
    sub.add_parser("status")
    s = sub.add_parser("resync", help="rebuild the record's driver state from the chain")
    s.add_argument("--launcher", help="the distributor's launcher id; default the record's")
    s.add_argument("--write", action="store_true", help="save the chain's state into the record")
    args = p.parse_args()
    clvm = Clvm()
    puzzles = Puzzles(clvm)
    if args.cmd == "resync":  # reads the chain only: no wallet needed
        cmd_resync(args, None, load(), puzzles)
        return 0
    wallet = Wallet(clvm)
    print(f"wallet {wallet.name}  node {NODE}  state {STATE_FILE.name}")
    state = load()
    {"launch": cmd_launch, "fund-roles": cmd_fund_roles, "create": cmd_create, "post": cmd_post, "claim": cmd_claim, "claim-all": cmd_claim_all,
     "reclaim": cmd_reclaim, "delegate": cmd_delegate, "rotate": cmd_rotate, "cancel-rotation": cmd_cancel_rotation, "dispute": cmd_dispute, "resolve": cmd_resolve,
     "status": cmd_status}[args.cmd](args, wallet, state, puzzles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
