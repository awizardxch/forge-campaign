#!/usr/bin/env python3
"""A local page showing FLPCR campaigns as the chain sees them.

    python scripts/dashboard.py            # http://127.0.0.1:8765

Three views, Nightspire-styled like the Forge site: the campaigns list (the
markets page's shape: stats, filters, a table with APR, TVL, a sparkline of
the emission, daily rewards and time left), a campaign's own page (APR card,
daily rewards and TVL, campaign information, rules, analytics) and a wallet
dashboard. Reads the state file deploy_testnet.py recorded (`FLPCR_STATE`;
earlier distributors' files are shown read-only) and asks the node for the
live facts. A development tool: its Claim and Claim all buttons sign through
the local Sage RPC (deploy_testnet.py's claim and claim-all), not WalletConnect.
The forge-ui tab will replace it.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import deploy_testnet as dep  # noqa: E402  (its node helpers and state file; importing runs no command)
from chia_wallet_sdk import Address, Clvm  # noqa: E402
from flpcr.chain.driver import HEIGHT_WINDOW, Distributor, Puzzles  # noqa: E402

PORT = int(os.environ.get("PORT") or 8765)
BLOCK_SECONDS = 18.75
BLOCKS_PER_DAY = 4608
# The testnet responder behind forge.awizard.dev's testnet markets: the live
# state of every real testnet pool, which is what a campaign's APR is priced on.
FORGE_API = os.environ.get("FLPCR_FORGE_API", "https://forge-responder.up.railway.app").rstrip("/")
XCH = "0" * 64
_pools_cache: dict = {"at": 0, "pools": {}}
_nft_cache: dict = {}
_wallet_cache: dict = {"at": 0, "index": {}}


def wallet_index() -> dict:
    """puzzle hash -> derivation index for the Sage wallet's first 64 addresses (cached 10 min)."""
    if time.time() - _wallet_cache["at"] > 600:
        try:
            w = dep.Wallet(Clvm())
            _wallet_cache.update({"at": time.time(), "index": dict(w.index_of)})
        except BaseException:  # Sage down: show nothing rather than fail the page
            _wallet_cache.update({"at": time.time(), "index": {}})
    return _wallet_cache["index"]


def wallet_claimable(state: dict, d) -> dict:
    """Per address of this wallet: what the claim-all button would pay right now."""
    try:
        root = dep.active_root(state, d)
    except SystemExit as e:
        return {"root": None, "why": str(e), "rows": []}
    idx = wallet_index()
    rows = []
    for cl in root["claims"]:
        ph = bytes.fromhex(cl["puzzle_hash"])
        if ph not in idx:
            continue
        rec = d.claims.get(d.claim_key(bytes.fromhex(cl["campaign"]), ph))
        owed = cl["cumulative"] - (rec.claimed if rec else 0)
        if owed > 0:
            rows.append({"derivation": idx[ph], "address": Address(ph, "txch").encode(), "campaign": cl["campaign"],
                         "amount": owed})
    return {"root": int(max(state["roots"], key=int)) if state.get("roots") else None, "why": None,
            "rows": sorted(rows, key=lambda r: (r["derivation"], r["campaign"]))}
DEXIE_API = os.environ.get("FLPCR_DEXIE_API", "https://api-testnet.dexie.space").rstrip("/")


def nft_stats(record: dict) -> dict:
    """For an NFT campaign: how many NFTs are eligible right now (the live
    crawl the engine uses: minted by the DID and, for a col1 collection,
    named by hash-verified metadata) and the collection's floor price (the
    cheapest open Dexie offer of one NFT for TXCH, in mojos). Cached two minutes."""
    key = record["id"]
    hit = _nft_cache.get(key)
    if hit and time.time() - hit["at"] < 120:
        return hit
    from flpcr.chain.nfts import ChainNftSource
    from flpcr.chain.node_reader import NodeReader
    from flpcr.model import BURN_PUZZLE_HASH
    target = bytes.fromhex(record["target"])
    coll = record.get("collection")
    out = {"at": time.time(), "nfts": None, "holders": None, "floor": None, "floor_error": None}
    try:
        src = ChainNftSource(NodeReader.connect(dep.NODE),
                             collections={target: (bytes.fromhex(coll[0]), coll[1])} if coll else {})
        held: dict = {}
        excluded = {bytes.fromhex(x) for x in (record.get("params") or [0, 0, 0, []])[3]} | {BURN_PUZZLE_HASH}
        for e in src.nft_events(target, dep.peak() + 1):
            held[e.puzzle_hash] = held.get(e.puzzle_hash, 0) + e.delta
        held = {k: v for k, v in held.items() if v and k not in excluded}
        out["nfts"], out["holders"] = sum(held.values()), len(held)
    except Exception as e:  # the page still renders
        out["floor_error"] = f"crawl: {type(e).__name__}"
    if coll:
        import urllib.request
        try:
            col = Address(target, "col").encode()
            url = f"{DEXIE_API}/v1/offers?offered={col}&requested=xch&status=0&sort=price&page_size=20"
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "flpcr-dashboard/1.0"}), timeout=20) as r:
                offers = json.loads(r.read()).get("offers", [])
            prices = [o["price"] for o in offers if len(o.get("offered") or []) == 1
                      and [x.get("id") for x in o.get("requested") or []] == ["xch"]]
            out["floor"] = int(round(min(prices) * 10**12)) if prices else None
        except Exception as e:
            out["floor_error"] = f"dexie: {type(e).__name__}"
    _nft_cache[key] = out
    return out


def forge_pools() -> dict:
    """launcher id -> {label, symbol, tvl (mojos of XCH), lp_supply, reserves}, cached a minute."""
    import urllib.request
    if time.time() - _pools_cache["at"] < 60:
        return _pools_cache["pools"]
    out = {}
    try:
        req = urllib.request.Request(f"{FORGE_API}/api/pools", headers={"User-Agent": "flpcr-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        for entry in data.get("pools", []):
            pool, st = entry["pool"], entry.get("state") or {}
            reserves = [int(x) for x in ((st.get("curriedState") or {}).get("state") or {}).get("reserves", [])]
            weights = pool.get("weights") or []
            ids = pool.get("assetIds") or []
            tvl = None
            if XCH in ids and reserves and weights and len(reserves) == len(ids) == len(weights):
                i = ids.index(XCH)
                # Every reserve is worth its weight's share of the pool, so the
                # whole pool is the XCH reserve over the XCH weight's share.
                tvl = int(reserves[i] * sum(weights) / weights[i])
            out[pool["launcherCoinId"]] = {
                "label": pool.get("label"), "symbol": pool.get("description") or pool.get("label"),
                "assets": pool.get("assets"), "version": pool.get("poolVersion"),
                "tvl": tvl, "lp_supply": st.get("totalLpSupply"), "reserves": reserves,
            }
    except Exception as e:  # the page still renders without prices
        out = {"_error": f"{type(e).__name__}: {e}"}
    _pools_cache.update({"at": time.time(), "pools": out})
    return out


def legacy_states() -> list:
    """Every other launched state file beside the primary one: earlier
    distributors whose campaigns are still on chain. Shown read-only; the
    driver only spends the primary distributor."""
    out = []
    for path in sorted(dep.ROOT.glob(f"state-{dep.NETWORK}*.json")):
        if path.resolve() == dep.STATE_FILE.resolve():
            continue
        try:
            s = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if "launcher_id" in s:
            s["_file"] = path.name
            out.append(s)
    return out


def legacy_distributor(state: dict, puzzles: Puzzles) -> Distributor:
    """The recorded driver snapshot without the puzzle-hash check: an older
    revision's puzzle does not rebuild, but its recorded chain state still reads."""
    launcher_id = bytes.fromhex(state["launcher_id"])
    cfg = dict(state["config"])
    cfg.setdefault("dispute_bond", 1)  # a pre-dispute revision has none; Config refuses 0 and this value is never spent
    d = Distributor(puzzles, dep.config_from({"config": cfg}, launcher_id), bytes.fromhex(state["engine_ph"]))
    d.restore(state["driver"])
    return d


def nft_label(record: dict):
    if record.get("kind") != 3:
        return None
    target = bytes.fromhex(record["target"])
    coll = record.get("collection")
    did = bytes.fromhex(coll[0]) if coll else target
    return {"label": Address(target, "col" if coll else "did:chia:").encode(),
            "did": Address(did, "did:chia:").encode(), "collection_id": coll[1] if coll else None}


def campaign_views(state: dict, d: Distributor, h: int, pools: dict, who, legacy: bool) -> list:
    st = d.chain_state
    roots = state.get("roots", {})
    records = dep.campaign_records(state)
    campaigns = []
    for c in st.campaigns:
        mine = records.get(c.id.hex(), {})
        pool = pools.get(mine.get("target", ""))
        length = c.end - c.start
        days = length / BLOCKS_PER_DAY
        holders = {}
        for rid, r in sorted(roots.items(), key=lambda kv: int(kv[0])):
            for cl in r["claims"]:
                if cl["campaign"] == c.id.hex():
                    holders[cl["puzzle_hash"]] = {"who": who(bytes.fromhex(cl["puzzle_hash"])), "cumulative": cl["cumulative"]}
        claimed_by = {}
        for entry in state.get("log", []):
            if entry.get("step") == "claim":
                claimed_by[entry["holder"]] = entry["cumulative"]
        campaigns.append({
            "id": c.id.hex(), "distributor": state["launcher_id"], "legacy": legacy,
            "creator": who(c.creator), "creator_address": Address(c.creator, "txch").encode(),
            "budget": c.budget, "claimed": c.claimed, "amount": mine.get("amount"), "nonce": mine.get("nonce"),
            "kind": {1: "LP_HOLD", 2: "CAT_HOLD", 3: "NFT_HOLD"}.get(mine.get("kind", 1), "CAT_HOLD"), "target": mine.get("target", ""),
            # An NFT campaign names a col1 collection (minting DID + metadata collection id) or a whole DID.
            "nft": nft_label(mine),
            "params": mine.get("params"),
            "start": c.start, "end": c.end, "length": length, "days": days, "claim_window": c.claim_window,
            "reclaim_from": c.end + c.claim_window,
            "daily": int(c.budget / days) if days else c.budget,
            "emittable_now": c.budget * (min(max(h, c.start), c.end) - c.start) // length,
            "phase": ("scheduled" if h < c.start else "running" if h < c.end
                      else "claim window" if h < c.end + c.claim_window else "reclaimable"),
            "holders": [{"who": v["who"], "cumulative": v["cumulative"], "claimed": claimed_by.get(v["who"], 0)}
                        for v in holders.values()],
            "roots": [{"root_id": int(k), **{x: v[x] for x in ("start", "end", "activates_at")},
                       "total": v["totals"].get(c.id.hex(), 0)} for k, v in sorted(roots.items(), key=lambda kv: int(kv[0])) if v["end"] > c.start],
            "revoked": [{"root_id": v["root_id"], "why": v["why"], "start": v["start"], "end": v["end"],
                         "total": v["totals"].get(c.id.hex(), 0)} for v in state.get("revoked_roots", [])],
            "pool": pool and {k: pool[k] for k in ("label", "symbol", "assets", "version", "lp_supply")},
            "tvl": pool and pool["tvl"],
            # yearly rewards over the pool's value: what every LP holder shares
            "apr": (pool and pool["tvl"] and days) and round(100 * (c.budget / days) * 365 / pool["tvl"], 2),
        })
        view = campaigns[-1]
        if view["kind"] == "NFT_HOLD" and not legacy:
            s = nft_stats({**mine, "id": c.id.hex()})
            per_day = (view["daily"] // s["nfts"]) if s["nfts"] else None
            view["nft"] = {**(view["nft"] or {}), "count": s["nfts"], "holders": s["holders"], "floor": s["floor"],
                           "per_nft_day": per_day, "per_nft_year": per_day and per_day * 365, "note": s["floor_error"]}
            # APR on an NFT: one NFT's yearly reward over what one NFT costs (the floor).
            if per_day and s["floor"]:
                view["apr"] = round(100 * per_day * 365 / s["floor"], 2)
    return campaigns


def status() -> dict:
    state = dep.load()
    if "launcher_id" not in state:
        return {"error": "no distributor launched yet"}
    puzzles = Puzzles(Clvm())
    try:
        d = dep.distributor_from(state, puzzles)
    except SystemExit:  # an earlier revision's puzzle: still readable from its record
        d = legacy_distributor(state, puzzles)
    st = d.chain_state
    h = dep.peak()
    singleton = dep.coin_record(d.coin.coin_id())
    reserve = dep.coin_record(d.reserve_coin.coin_id())
    names = {bytes.fromhex(v["puzzle_hash"]): k for k, v in state["roles"].items()}
    roles = {}
    for role, info in state["roles"].items():
        ph = bytes.fromhex(info["puzzle_hash"])
        roles[role] = {"address": info["address"], "balance": sum(c.amount for c in dep.unspent_at([ph]))}

    def who(ph: bytes) -> str:
        return names.get(ph, ph.hex()[:12])

    def root(r):
        if r is None:
            return None
        return {"root_id": r.root_id, "start": r.start, "end": r.end, "root": r.root.hex(),
                "totals": {k.hex(): v for k, v in r.totals}, "activates_at": r.activates_at}

    pools = forge_pools()
    campaigns = campaign_views(state, d, h, pools, who, legacy=False)
    legacy = []
    for old in legacy_states():
        try:
            od = legacy_distributor(old, puzzles)
        except Exception as e:  # an unreadable record hides only itself
            legacy.append({"file": old["_file"], "error": str(e)})
            continue
        old_names = {bytes.fromhex(v["puzzle_hash"]): k for k, v in old["roles"].items()}
        campaigns += campaign_views(old, od, h, pools, lambda ph: old_names.get(ph, ph.hex()[:12]), legacy=True)
        legacy.append({"file": old["_file"], "launcher_id": old["launcher_id"], "campaigns": len(od.chain_state.campaigns)})
    campaigns.sort(key=lambda c: c["start"])
    return {
        "legacy": legacy,
        "network": state["network"], "launcher_id": state["launcher_id"], "wallet": state.get("wallet"),
        "peak": h, "block_seconds": BLOCK_SECONDS, "time": int(time.time()),
        "singleton": {"coin_id": d.coin.coin_id().hex(), "unspent": bool(singleton and not singleton["spent"])},
        "reserve": {"state": st.reserve, "chain": reserve and reserve["coin"]["amount"]},
        "engine": who(st.engine_ph),
        "rotation": st.rotation and {"engine_ph": who(st.rotation.engine_ph), "effective_at": st.rotation.effective_at},
        "dispute": st.dispute and {"root_id": st.dispute.root_id, "disputer": who(st.dispute.disputer_ph),
                                   "bond": state["config"].get("dispute_bond")},
        "config": state["config"], "roles": roles,
        "campaigns": campaigns, "active": root(st.active), "pending": root(st.pending),
        "claim_slots": len(d.claims) - 2,
        "delegations": [{"key": k.hex()[:12], "active": v.active, "uses": v.uses} for k, v in d.delegations.items()
                        if k not in (bytes(32), b"\xff" * 32)],
        "dispute_total": HEIGHT_WINDOW + state["config"]["dispute_blocks"],
        "log": state.get("log", [])[-30:],
        "resolutions": [{"root_id": e.get("root_id"), "upheld": e.get("upheld"), "height": e.get("height"), "time": e["time"]}
                        for e in state.get("log", []) if e.get("step") == "resolve"],
        "wallets": [{"who": r, "address": roles[r]["address"]} for r in ("holder", "delegate", "creator")],
        "wallet_claimable": wallet_claimable(state, d),
        "forge_api": FORGE_API, "pools_error": pools.get("_error"),
        "claims": [{"who": e["holder"], "cumulative": e["cumulative"], "height": e.get("height"), "time": e["time"]}
                   for e in state.get("log", []) if e.get("step") == "claim"]
                  + [{"who": "wallet", "cumulative": e.get("cumulative", 0), "height": e.get("height"), "time": e["time"],
                      "addresses": len(e.get("holders") or [])}
                     for e in state.get("log", []) if e.get("step") == "claim-all"],
    }


CLAIM_LOCK = threading.Lock()
CLAIM_LOG: list = []


def run_claim(holder: str, campaign: str) -> None:
    """The holder's claim through deploy_testnet, in a thread; progress in CLAIM_LOG."""
    class Args:
        delegated = False
        by_key = False
    Args.holder = holder
    Args.campaign = campaign
    clvm = Clvm()
    try:
        with CLAIM_LOCK:
            CLAIM_LOG.append({"time": int(time.time()), "holder": holder, "status": "building"})
            dep.cmd_claim(Args(), dep.Wallet(clvm), dep.load(), Puzzles(clvm))
            CLAIM_LOG.append({"time": int(time.time()), "holder": holder, "status": "confirmed"})
    except SystemExit as e:
        CLAIM_LOG.append({"time": int(time.time()), "holder": holder, "status": f"refused: {e}"})
    except Exception as e:  # noqa: BLE001
        CLAIM_LOG.append({"time": int(time.time()), "holder": holder, "status": f"error: {type(e).__name__}: {e}"})


def run_claim_all() -> None:
    """Every address of the Sage wallet with a leaf in the active root, by key
    signature, in one bundle (deploy_testnet claim-all); progress in CLAIM_LOG."""
    class Args:
        campaign = None
    clvm = Clvm()
    try:
        with CLAIM_LOCK:
            CLAIM_LOG.append({"time": int(time.time()), "holder": "wallet", "status": "building"})
            dep.cmd_claim_all(Args(), dep.Wallet(clvm), dep.load(), Puzzles(clvm))
            CLAIM_LOG.append({"time": int(time.time()), "holder": "wallet", "status": "confirmed"})
    except SystemExit as e:
        CLAIM_LOG.append({"time": int(time.time()), "holder": "wallet", "status": f"refused: {e}"})
    except Exception as e:  # noqa: BLE001
        CLAIM_LOG.append({"time": int(time.time()), "holder": "wallet", "status": f"error: {type(e).__name__}: {e}"})


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Forge Campaigns</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--text-color:#e0f7ff;--border-color:#00d9ff;--glow-inner:#ff6600;--glow-outer:#00d9ff;--sel-fill:#d7fcfe;--sel-text:#020d0d;
--border-w:2.5px;--glow-r:10;--glow-s:30;--glow-far:#1a082b;--glow-f:36;--glow-b1:5;--glow-b2:8;
--bg:rgba(10,12,24,0.88);--bg-deep:#060810;--bg-card:rgba(25,15,20,0.93);--bg-tertiary:rgba(35,25,35,0.9);
--text-muted:#8ba3b0;--accent:#00d9ff;--success:#4ade80;--danger:#f23f42;--warning:#f0b232;--line:rgba(0,217,255,0.18)}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);background-color:var(--bg-deep);color:var(--text-color);
font:15px/1.5 'gg sans','Noto Sans',Helvetica,Arial,sans-serif;padding:0 0 60px;overflow-x:hidden}
a{color:var(--accent);text-decoration:none}
.top{display:flex;align-items:center;gap:18px;padding:16px max(18px,env(safe-area-inset-left));border-bottom:1px solid var(--line)}
.top .brand{font-weight:800;font-size:1.3rem;letter-spacing:.02em;text-shadow:0 0 6px #fff,0 0 18px rgba(255,255,255,.6),0 0 40px var(--glow-inner)}
.top .nav{display:flex;gap:16px;margin-left:auto;color:var(--text-muted);font-size:14px}.top .nav b{color:var(--text-color)}
.page{padding:22px max(18px,env(safe-area-inset-left));max-width:1240px;margin:0 auto}
h1{font-size:2rem;margin:0 0 4px;line-height:1.15}h1 .pct{color:var(--accent)}
.sub{color:var(--text-muted);margin:0 0 14px}
h2{font-size:.78rem;margin:0 0 10px;color:var(--accent);text-transform:uppercase;letter-spacing:.12em}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:18px;margin:18px 0}
.stat{background:var(--bg-card);border:1px solid var(--line);border-radius:10px;padding:12px 16px}.stat .l{color:var(--text-muted);font-size:13px}.stat .v{font-size:1.5rem;font-weight:700;color:#fff}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.chip{border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:13px;color:var(--text-color);background:var(--bg-deep);cursor:pointer}
.chip.on{background:var(--sel-fill);color:var(--sel-text);border-color:var(--border-color)}
.rule{font-family:ui-monospace,Consolas,monospace;font-size:12px;border:1px solid var(--line);border-radius:6px;padding:3px 8px;color:var(--text-color);background:var(--bg-deep)}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:12px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--text-muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.08em}
tr.row{cursor:pointer}tr.row:hover td{background:rgba(0,217,255,.06)}
td.n,th.n{text-align:right;font-family:ui-monospace,Consolas,monospace}
.name{font-weight:600;font-size:15px}.tags{color:var(--text-muted);font-size:12px;margin-top:2px}
.apr{color:#a78bfa;font-weight:700}
.card{background:var(--bg-card);border:var(--border-w) solid var(--border-color);border-radius:12px;padding:16px 18px;min-width:0;
box-shadow:0 0 calc(var(--glow-r)*1px) var(--glow-inner),0 0 calc(var(--glow-s)*1px) calc(var(--glow-b1)*1px) var(--glow-outer),0 0 calc(var(--glow-f)*1px) calc(var(--glow-b2)*1px) var(--glow-far)}
.flat{background:var(--bg-card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
.detail{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:24px;align-items:start}
@media(max-width:900px){.detail{grid-template-columns:1fr}}
.side{display:grid;gap:16px}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;font-size:14px;margin:0}.kv dt{color:var(--text-muted)}.kv dd{margin:0;overflow-wrap:anywhere;font-family:ui-monospace,Consolas,monospace;font-size:13px}
.big{font-size:2rem;font-weight:800;color:#a78bfa;text-shadow:0 0 14px rgba(167,139,250,.6)}
.mute{color:var(--text-muted)}.ok{color:var(--success)}.warn{color:var(--warning)}.bad{color:var(--danger)}
.bar{height:8px;background:var(--bg-deep);border:1px solid var(--line);border-radius:5px;overflow:hidden;margin:10px 0}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--glow-inner),var(--glow-outer));box-shadow:0 0 10px var(--glow-outer)}
.btn{display:block;width:100%;text-align:center;padding:14px;border-radius:10px;font-weight:700;font-size:1.05rem;color:var(--text-color);
background:linear-gradient(90deg,var(--bg) 60%,var(--bg-deep) 100%);border:var(--border-w) solid var(--border-color);
box-shadow:0 0 calc(var(--glow-r)*1px) var(--glow-inner),0 0 calc(var(--glow-s)*1px) calc(var(--glow-b1)*1px) var(--glow-outer);cursor:pointer}
.btn.primary{background:#7c5cff;border-color:#9d85ff;color:#fff}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:700px){.two{grid-template-columns:1fr}}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;border:1px solid var(--line);color:var(--text-muted)}
.pill.run{color:var(--success);border-color:rgba(74,222,128,.4)}.pill.wait{color:var(--warning)}
svg.chart{width:100%;height:220px;display:block}svg.spark{width:110px;height:28px;display:block}
.legend{display:flex;gap:16px;font-size:12px;color:var(--text-muted);margin-top:6px}.legend i{display:inline-block;width:10px;height:3px;vertical-align:middle;margin-right:6px}
</style></head><body>
<div class="top"><span class="brand">Forge</span><span class="mute">Campaigns</span>
<div class="nav"><span>Markets</span><span>Pools</span><a href="#/" id="navc">Campaigns</a><a href="#/dashboard" id="navd">Dashboard</a><span id="net" class="mute"></span>
<select id="wallet" class="chip" style="margin-left:8px"><option value="">Connect wallet</option></select></div></div>
<div class="page" id="page">loading</div>
<script>
const $=s=>document.querySelector(s);
const fmt=n=>n==null?"-":Number(n).toLocaleString();
const txch=n=>n==null?"-":(n/1e12).toFixed(n>=1e11?3:6)+" TXCH";
const apr=c=>c.apr!=null?c.apr.toFixed(2)+"%":(c.nft&&c.nft.per_nft_day?txch(c.nft.per_nft_day)+"/NFT/day":"\u2014");
const pname=c=>c.pool?`${c.pool.symbol||c.pool.label}`:c.nft?(c.nft.collection_id?`collection ${c.nft.label.slice(0,12)}\u2026`:`DID ${c.nft.label.slice(9,19)}\u2026`):`${c.target.slice(0,10)}\u2026`;
const BS=__BS__;
const rel=(n,now)=>{const d=n-now;const m=Math.abs(d)*BS/60;const s=m>=120?(m/60).toFixed(1)+" h":Math.round(m)+" min";return d>=0?s+" left":s+" ago"};
let S=null,view=location.hash;
function phase(c){return `<span class="pill ${c.phase==="running"?"run":c.phase==="scheduled"?"wait":""}">${c.phase}</span>`}
function spark(c,h){ // the emission schedule and the posted totals, in the sparkline slot
  const w=110,ht=28,x=b=>w*(b-c.start)/(c.end-c.start);
  const sched=`M0,${ht} L${w},0`;
  let posted=`M0,${ht}`;for(const r of c.roots){posted+=` L${x(Math.min(r.end,c.end)).toFixed(1)},${(ht-ht*r.total/c.budget).toFixed(1)}`}
  const now=Math.min(Math.max(x(h),0),w);
  return `<svg class="spark" viewBox="0 0 ${w} ${ht}"><path d="${sched}" stroke="#4ade80" stroke-width="1.5" fill="none" opacity=".5"/><path d="${posted}" stroke="#00d9ff" stroke-width="1.5" fill="none"/><line x1="${now}" y1="0" x2="${now}" y2="${ht}" stroke="#8ba3b0" stroke-dasharray="2 2"/></svg>`}
function chart(c,h){ // released by schedule, posted (claimable), claimed; x in blocks
  const W=760,H=220,L=56,B=26,R=12,T=10,x=b=>L+(W-L-R)*(b-c.start)/(c.end-c.start),y=v=>T+(H-T-B)*(1-v/c.budget);
  // Gridlines at round values (1, 2 or 5 x 10^k TXCH), labeled with the decimals that step needs.
  const top=c.budget/1e12,raw=top/4,mag=Math.pow(10,Math.floor(Math.log10(raw))),step=[1,2,5,10].map(m=>m*mag).find(s=>s>=raw);
  const dec=Math.max(0,-Math.floor(Math.log10(step)));
  const ticks=Array.from({length:Math.floor(top/step+1e-9)+1},(_,i)=>i*step*1e12).map(v=>`<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="rgba(0,217,255,.12)"/><text x="${L-6}" y="${y(v)+4}" text-anchor="end" fill="#8ba3b0" font-size="11">${(v/1e12).toFixed(dec)}</text>`).join("");
  const hours=[0,1,2,3,4].map(k=>c.start+k*(c.end-c.start)/4).map((b,i)=>`<text x="${x(b)}" y="${H-8}" text-anchor="${i==0?"start":i==4?"end":"middle"}" fill="#8ba3b0" font-size="11">${fmt(Math.round(b))}</text>`).join("");
  const sched=`M${x(c.start)},${y(0)} L${x(c.end)},${y(c.budget)}`;
  // What the roots have made claimable: a step up at each root's end. A root that ended before
  // the campaign started carries nothing for it and is skipped.
  let posted=`M${x(c.start)},${y(0)}`,prev=0,lx=x(c.start);
  for(const r of c.roots){if(r.end<=c.start)continue;const xe=x(Math.min(r.end,c.end));posted+=` L${xe},${y(prev)} L${xe},${y(r.total)}`;prev=r.total;lx=xe}
  const xn=Math.min(Math.max(x(h),x(c.start)),x(c.end));if(xn>lx)posted+=` L${xn},${y(prev)}`;
  const claimedY=y(c.claimed);
  const now=Math.min(Math.max(x(h),x(c.start)),x(c.end));
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${ticks}${hours}
   <path d="${sched}" stroke="#4ade80" stroke-width="2" fill="none" opacity=".6"/>
   <path d="${posted}" stroke="#00d9ff" stroke-width="2.5" fill="none"/>
   <line x1="${L}" x2="${now}" y1="${claimedY}" y2="${claimedY}" stroke="#ff6600" stroke-width="2"/>
   <line x1="${now}" x2="${now}" y1="${T}" y2="${H-B}" stroke="#8ba3b0" stroke-dasharray="4 3"/><text x="${now}" y="${T+10}" fill="#8ba3b0" font-size="11" text-anchor="middle">now ${fmt(h)}</text></svg>
   <div class="legend"><span><i style="background:#4ade80"></i>released by schedule</span><span><i style="background:#00d9ff"></i>posted in roots (claimable)</span><span><i style="background:#ff6600"></i>claimed</span></div>`}
function list(){
  const h=S.peak,cs=S.campaigns,budget=cs.reduce((a,c)=>a+c.budget,0),daily=cs.filter(c=>c.phase==="running").reduce((a,c)=>a+c.daily,0);
  return `<h1>More yield on what you hold</h1><p class="sub">Pool creators allocate rewards. Forge calculates each holder's share from past holdings and pays it from the distributor's reserve.</p>
  <div class="stats"><div class="stat"><div class="l">Campaigns</div><div class="v">${cs.length}</div></div><div class="stat"><div class="l">Total budget</div><div class="v">${txch(budget)}</div></div>
  <div class="stat"><div class="l">Daily rewards (running)</div><div class="v">${txch(daily)}</div></div><div class="stat"><div class="l">Reserve on chain</div><div class="v">${txch(S.reserve.chain)}</div></div></div>
  <div class="chips"><span class="chip on">All</span><span class="chip">Provide liquidity</span><span class="chip">Hold token</span><span class="chip">Running</span><span class="chip">Ended</span><span class="chip" style="margin-left:auto">Daily rewards ↓</span></div>
  <table><tr><th>${cs.length} opportunities</th><th class="n">APR</th><th class="n">TVL</th><th>Emission</th><th class="n">Daily rewards</th><th>Time left</th></tr>
  ${cs.map(c=>`<tr class="row" onclick="location.hash='#/campaign/${c.id}'"><td><div class="name">${c.kind==="LP_HOLD"?"Provide liquidity to":c.kind==="NFT_HOLD"?"Hold an NFT from":"Hold"} ${pname(c)}</div><div class="tags">testnet11 · Forge${c.pool?" V"+(c.pool.version-1):""} · ${c.creator} ${phase(c)}${c.legacy?' <span class="mute">· earlier distributor</span>':""}</div></td>
   <td class="n apr">${apr(c)}</td><td class="n">${c.tvl?txch(c.tvl):"unpriced"}</td><td>${spark(c,h)}</td><td class="n">${txch(c.daily)}<div class="mute" style="font-size:12px">${txch(c.budget)} total</div></td><td>${c.phase==="scheduled"?"starts in "+rel(c.start,h).replace(" left",""):c.phase==="running"?rel(c.end,h):"ended · "+(c.phase==="claim window"?"reclaim "+rel(c.reclaim_from,h):"reclaimable")}</td></tr>`).join("")||"<tr><td colspan=6 class='mute'>no campaigns yet</td></tr>"}</table>`}
function detail(id){
  const c=S.campaigns.find(x=>x.id===id);if(!c)return `<p class="mute">no such campaign</p>`;
  const h=S.peak,total=c.end-c.start,done=Math.min(Math.max(h-c.start,0),total),pct=100*done/total;
  const p=c.params||[];const timeBps=p[0]??10000,feesBps=p[1]??0;
  const rows=[["Dates",`blocks ${fmt(c.start)} → ${fmt(c.end)} (${c.days.toFixed(2)} days)`],["Reward chain","testnet11"],["Distribution strategy","Linear schedule, equal every day; shares from past holdings"],["Total campaign budget",`${txch(c.budget)} <span class="mute">(${txch(c.amount)} less the 3% dev fee)</span>`],["Campaign ID",c.id.slice(0,12)+"…"+c.id.slice(-6)],["Creator",c.creator_address.slice(0,22)+"…"],["Claim window",`${c.claim_window} blocks after the end; reclaim from ${fmt(c.reclaim_from)}`]];
  return `<a href="#/" class="mute">← Campaigns</a><h1>${c.kind==="LP_HOLD"?"Provide liquidity to":c.kind==="NFT_HOLD"?"Hold an NFT from":"Hold"} ${c.nft?"":"Forge "}${pname(c)} <span class="pct">${c.apr!=null?apr(c):(c.kind==="LP_HOLD"?"LP":c.kind==="NFT_HOLD"?"NFT":"CAT")}</span></h1>
  <p class="sub">Earn ${txch(c.daily)} per day by ${c.kind==="LP_HOLD"?"holding the pool's LP":c.kind==="NFT_HOLD"?"holding NFTs of the collection (one share each)":"holding the token"} on testnet11. ${phase(c)}</p>
  <div class="detail"><div>
   <div class="flat" style="margin-bottom:16px"><div style="display:flex;justify-content:space-between;align-items:baseline"><div><b>${txch(c.daily)}</b> <span class="mute">/day</span></div><div class="apr">APR ${apr(c)}</div><div class="mute">${c.phase==="running"?rel(c.end,h):c.phase==="scheduled"?"starts in "+rel(c.start,h).replace(" left",""):c.phase}</div></div>
   <div class="bar"><i style="width:${pct.toFixed(1)}%"></i></div>${c.legacy?`<div class="mute" style="font-size:13px;margin-top:6px">Runs on an earlier distributor revision: shown from its record; the live driver spends the current distributor only.</div>`:""}${S.dispute&&!c.legacy?`<div class="warn">Root ${S.dispute.root_id} is disputed by ${S.dispute.disputer} with a ${txch(S.dispute.bond)} bond; frozen until the guardian or admin resolves it.</div>`:""}${!c.legacy&&S.resolutions.length?`<div class="mute" style="font-size:13px;margin-top:6px">Disputes resolved: ${S.resolutions.map(r=>`root ${r.root_id} ${r.upheld?"upheld (bond refunded, root revoked)":"rejected (bond forfeited to the dev fee, claim window restarted)"}`).join("; ")}.</div>`:""}</div>
   <div class="two"><div class="flat"><h2>Campaign information</h2><dl class="kv">${rows.map(([k,v])=>`<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl></div>
   <div class="flat"><h2>Rules</h2><div class="chips">${c.nft?`<span class="rule">${c.nft.collection_id?"Collection "+c.nft.label.slice(0,14)+"…":"Every NFT minted by the DID"}</span><span class="rule">Minted by ${c.nft.did.slice(0,16)}…</span>${c.nft.collection_id?`<span class="rule">Metadata collection ${c.nft.collection_id.slice(0,8)}…</span>`:""}<span class="rule">1 share per NFT</span>`:`<span class="rule">Pool ${c.target.slice(0,8)}…</span>`}${c.pool?`<span class="rule">${c.pool.assets.join(" / ")}</span><span class="rule">LP supply ${fmt(c.pool.lp_supply)}</span>`:""}<span class="rule">Distributor ${c.distributor.slice(0,8)}…${c.legacy?" (earlier revision)":""}</span><span class="rule">Time share ${(timeBps/100).toFixed(0)}%</span><span class="rule">Fee share ${(feesBps/100).toFixed(0)}%</span><span class="rule">Dispute ${S.dispute_total} blocks</span><span class="rule">Claim by coin spend</span></div>
   <h2 style="margin-top:14px">Holders in the latest root</h2>${c.holders.length?`<table>${c.holders.map(x=>`<tr><td>${x.who}</td><td class="n">${txch(x.cumulative)} earned</td><td class="n">${txch(x.claimed)} claimed</td></tr>`).join("")}</table>`:"<span class='mute'>no root posted yet</span>"}</div></div>
   <h2 style="margin-top:22px">Analytics</h2><div class="flat">${chart(c,h)}</div>
   <h2 style="margin-top:22px">Roots</h2><div class="flat">${c.roots.length?`<table><tr><th>#</th><th>window</th><th class="n">total</th><th>claimable from</th></tr>${c.roots.map(r=>`<tr><td>${r.root_id}</td><td>[${fmt(r.start)}, ${fmt(r.end)})</td><td class="n">${txch(r.total)}</td><td>${fmt(r.activates_at)} (${rel(r.activates_at,h)})</td></tr>`).join("")}</table>`:"<span class='mute'>the operator posts the first root about an hour in</span>"}${(c.revoked||[]).length?`<div class="mute" style="font-size:13px;margin-top:8px">Revoked: ${c.revoked.map(r=>`root ${r.root_id} [${fmt(r.start)}, ${fmt(r.end)}) for ${txch(r.total)}, ${r.why}; reposted under the same id`).join("; ")}</div>`:""}</div>
  </div><div class="side">
   <div class="card"><div class="mute">APR</div><div class="big">${apr(c)}</div><div class="mute" style="font-size:13px">${c.apr!=null?`${txch(c.daily)} a day over the pool's ${txch(c.tvl)}, annualized: what every LP holder shares in proportion to their time-averaged stake`:c.nft?(c.nft.count?`${txch(c.daily)} a day across ${c.nft.count} eligible NFT${c.nft.count==1?"":"s"} (${c.nft.holders} holder${c.nft.holders==1?"":"s"}): ${txch(c.nft.per_nft_day)} per NFT a day, ${txch(c.nft.per_nft_year)} a year. ${c.nft.floor?`Over the ${txch(c.nft.floor)} floor (cheapest Dexie offer) that is the APR shown.`:`No TXCH offers are listed on Dexie for this collection, so there is no floor price to make it a percentage.`}`:"no eligible NFTs read yet"):"needs the target pool's TVL; this campaign targets a label, not a priced pool"}</div></div>
   <div class="two"><div class="flat"><div class="mute">Daily rewards</div><div style="font-size:1.3rem;font-weight:700">${txch(c.daily)}</div></div><div class="flat"><div class="mute">TVL</div><div style="font-size:1.3rem;font-weight:700">${c.tvl?txch(c.tvl):"—"}</div></div></div>
   <div class="flat"><b>How to participate</b><div class="mute" style="font-size:13px">Hold the target in your wallet. Shares are computed from past blocks, time-averaged, so a position held a block or two earns nothing. Claim any time against the active root by spending a coin at your address; rewards go to that address.</div></div>
   <div class="flat"><b>Released so far</b><div>${txch(c.emittable_now)} <span class="mute">of ${txch(c.budget)}</span></div><div class="mute" style="font-size:13px">claimed ${txch(c.claimed)}</div></div>
   ${c.nft?`<a class="btn primary" href="https://testnet11.spacescan.io/${c.nft.collection_id?"en/collection/"+c.nft.label:"profile/"+c.nft.did}" target="_blank" rel="noopener">View the collection</a>`:`<a class="btn primary" href="#">Supply via Forge</a>`}<a class="btn" href="#">Claim rewards</a>
  </div></div>`}
function earned(c,who){const x=c.holders.find(x=>x.who===who);return x?x.cumulative:0}
function claimable(c,who,h){if(c.legacy)return 0;const x=c.holders.find(x=>x.who===who);if(!x)return 0;if(!c.roots.some(r=>r.activates_at<=h))return 0;return Math.max(0,x.cumulative-x.claimed)}
function pending(c,who,h){if(c.legacy)return 0;return Math.max(0,earned(c,who)-claimable(c,who,h)-(c.holders.find(x=>x.who===who)?.claimed||0))}
function dashboard(){
  const who=$("#wallet").value;if(!who)return `<h1>Your dashboard</h1><p class="sub">Connect a wallet to see what you have earned and claim it.</p><div class="flat"><div class="chips">${S.wallets.map(w=>`<span class="chip" onclick="$('#wallet').value='${w.who}';render()">${w.who} \u00b7 ${w.address.slice(0,14)}\u2026</span>`).join("")}</div></div>`;
  const h=S.peak,cs=S.campaigns,tot=cs.reduce((a,c)=>a+earned(c,who),0),now=cs.reduce((a,c)=>a+claimable(c,who,h),0),pend=cs.reduce((a,c)=>a+pending(c,who,h),0);
  const hist=S.claims.filter(x=>x.who===who||x.who==="wallet").slice().sort((a,b)=>b.time-a.time);
  return `<div style="display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:16px"><div><h1>Your dashboard</h1><p class="sub">Claim rewards you earned by holding. Wallet <b>${who}</b> \u00b7 ${S.wallets.find(w=>w.who===who).address.slice(0,22)}\u2026</p></div>
  <div style="text-align:right"><div class="mute" style="letter-spacing:.1em;font-size:12px">TOTAL EARNED</div><div class="big">${txch(tot)}</div>
  <div class="chips" style="justify-content:flex-end"><span class="chip ok" style="border-color:rgba(74,222,128,.5)">Claimable now ${txch(now)}</span><span class="chip">Pending ${txch(pend)}</span></div></div></div>
  ${cs.map(c=>{const cl=claimable(c,who,h);return `<div class="card" style="display:flex;align-items:center;gap:18px;max-width:560px;margin:12px 0"><div><div class="mute">testnet11 \u00b7 ${pname(c)}${c.legacy?" \u00b7 earlier distributor, read-only":""}</div><div style="font-size:1.4rem;font-weight:700">${txch(cl)}</div><div class="mute" style="font-size:12px">earned ${txch(earned(c,who))} \u00b7 ${c.phase}</div></div>
   <button class="btn primary" style="width:auto;margin-left:auto;padding:10px 26px;${cl>0&&!c.legacy?"":"opacity:.45"}" ${cl>0&&!c.legacy?"":"disabled"} title="${c.legacy?"earlier distributor: not spendable by this driver":""}" onclick="claim('${who}','${c.id}')">Claim</button></div>`}).join("")}
  <div class="card" style="display:flex;align-items:center;gap:18px;max-width:560px;margin:12px 0"><div style="flex:1"><b>Every address in this wallet</b> <span class="mute" style="font-size:12px">(dev: signs with the local Sage RPC)</span><div class="mute" style="font-size:12px">One approval signs the claims of all its addresses (none needs a coin); one bundle pays them.</div>
  ${(()=>{const wc=S.wallet_claimable||{rows:[]};if(!wc.rows.length)return `<div style="margin-top:8px;font-size:13px">Nothing to claim right now${wc.why?" ("+wc.why+")":" \u2014 everything in the active root is already claimed; the next root brings more"}.</div>`;const tot=wc.rows.reduce((a,r)=>a+r.amount,0);return `<div style="margin-top:8px;font-size:13px"><b>${txch(tot)}</b> claimable now across ${new Set(wc.rows.map(r=>r.derivation)).size} address(es):</div><table style="margin-top:4px">${wc.rows.map(r=>`<tr><td>derivation ${r.derivation}</td><td class="mute">${r.address.slice(0,16)}\u2026</td><td class="mute">${r.campaign.slice(0,8)}</td><td class="n">${txch(r.amount)}</td></tr>`).join("")}</table>`})()}
  <div id="claimallstatus" style="margin-top:8px;font-size:13px;color:var(--warning)"></div></div><button class="btn primary" style="width:auto;margin-left:auto;padding:10px 22px;align-self:flex-start" onclick="claimAll()" ${(S.wallet_claimable||{rows:[]}).rows.length?"":"disabled title='nothing to claim right now'"}>Claim all</button></div>
  <div id="claimlog" class="mute" style="font-size:13px"></div>
  <h2 style="margin-top:22px">Opportunities</h2><table><tr><th>Name</th><th>Protocol</th><th class="n">Earned</th><th class="n">Your position</th><th class="n">APR</th></tr>
  ${cs.map(c=>`<tr class="row" onclick="location.hash='#/campaign/${c.id}'"><td>${c.kind==="LP_HOLD"?"Provide liquidity to":c.kind==="NFT_HOLD"?"Hold an NFT from":"Hold"} ${pname(c)}</td><td>Forge</td><td class="n">${txch(earned(c,who))}</td><td class="n">${c.holders.some(x=>x.who===who)?"in the root":"\u2014"}</td><td class="n apr">${apr(c)}</td></tr>`).join("")}</table>
  <div class="two" style="margin-top:22px"><div><h2>Rewards</h2><table><tr><th>Campaign</th><th class="n">Pending</th><th class="n">Claimable now</th></tr>${cs.map(c=>`<tr><td>${pname(c)}<div class="mute" style="font-size:12px">${c.legacy?"earlier distributor \u00b7 ":""}last root ${c.roots.length?fmt(c.roots[c.roots.length-1].end):"none"}</div></td><td class="n">${txch(pending(c,who,h))}</td><td class="n ok">${txch(claimable(c,who,h))}</td></tr>`).join("")}</table></div>
  <div><h2>Claim history</h2>${hist.length?`<table><tr><th>Tx details</th><th class="n">Height</th><th>Date</th></tr>${hist.map(x=>`<tr><td>${x.who==="wallet"?`claim all: ${x.addresses} claim${x.addresses==1?"":"s"}, cumulative ${txch(x.cumulative)}`:`cumulative ${txch(x.cumulative)}`}</td><td class="n">${fmt(x.height)}</td><td>${new Date(x.time*1000).toLocaleString()}</td></tr>`).join("")}</table>`:"<span class='mute'>no claims yet</span>"}</div></div>`}
async function claimAll(){const r=await (await fetch("/api/claim-all",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})).json();const el=$("#claimallstatus");if(el)el.textContent=r.status;setTimeout(pollClaimAll,4000)}
async function pollClaimAll(){const l=await (await fetch("/api/claims")).json();const x=l.filter(e=>e.holder==="wallet").pop();const el=$("#claimallstatus");if(x&&el)el.textContent=x.status==="building"?"building and signing the bundle\u2026":x.status==="confirmed"?"claimed \u2014 confirmed on chain":x.status;if(x&&x.status==="building")setTimeout(pollClaimAll,8000);else load()}
async function claim(who,campaign){const r=await (await fetch("/api/claim",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({holder:who,campaign})})).json();$("#claimlog").textContent=r.status;setTimeout(pollClaims,5000)}
async function pollClaims(){const l=await (await fetch("/api/claims")).json();if(l.length){const x=l[l.length-1];const el=$("#claimlog");if(el)el.textContent=`${x.holder}: ${x.status}`;if(x.status==="building")setTimeout(pollClaims,10000);else load()}}
function render(){view=location.hash;$("#net").textContent=S?`${S.network} · peak ${fmt(S.peak)}`:"";
  const sel=$("#wallet");if(sel.options.length===1)for(const w of S.wallets){const o=document.createElement("option");o.value=w.who;o.textContent=`${w.who} ${w.address.slice(0,10)}…`;sel.appendChild(o)}
  const m=view.match(/^#\/campaign\/([0-9a-f]+)/);$("#page").innerHTML=view.startsWith("#/dashboard")?dashboard():m?detail(m[1]):list();
  $("#navd").style.color=view.startsWith("#/dashboard")?"#fff":"";$("#navc").style.color=view.startsWith("#/dashboard")?"":"#fff"}
async function load(){S=await (await fetch("/api/status")).json();if(S.error){$("#page").textContent=S.error;return}render()}
window.addEventListener("hashchange",render);$("#wallet").addEventListener("change",render);load();setInterval(load,30000);
</script></body></html>""".replace("__BS__", str(BLOCK_SECONDS))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/claim-all":
            if CLAIM_LOCK.locked():
                out = {"ok": False, "status": "a claim is already in flight"}
            else:
                threading.Thread(target=run_claim_all, daemon=True).start()
                out = {"ok": True, "status": "one signature request for every address; it confirms in a minute or two"}
        elif self.path == "/api/claim" and body.get("holder") in ("holder", "delegate", "creator"):
            if CLAIM_LOCK.locked():
                out = {"ok": False, "status": "a claim is already in flight"}
            else:
                threading.Thread(target=run_claim, args=(body["holder"], body.get("campaign")), daemon=True).start()
                out = {"ok": True, "status": "claim started; it confirms in a minute or two"}
        else:
            out = {"ok": False, "status": "unknown action"}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/claims"):
            body = json.dumps(CLAIM_LOG[-10:]).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if self.path.startswith("/api/status"):
            try:
                body = json.dumps(status()).encode()
            except Exception as e:  # surface, never crash the server
                body = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
            ctype = "application/json"
        else:
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


if __name__ == "__main__":
    print(f"FLPCR dashboard on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
