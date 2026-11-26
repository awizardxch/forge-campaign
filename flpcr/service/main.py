"""Run the campaign service: python -m flpcr.service.main

Environment (the host's service variables):

    FLPCR_NETWORK        testnet11 | mainnet                          (required)
    FLPCR_LAUNCHER       the primary distributor's launcher id, hex. Its Config and operator are
                         the build pin (flpcr.build_pin) every other distributor this service
                         hosts must match; it must name this host's keys. Unset: template mode.
    Template mode (no FLPCR_LAUNCHER; mainnet, 2026-11-25): the build pin comes from these and
    the production Config defaults, and the first creator's campaign launches the first
    distributor (flpcr.service.launch), found on chain by its hint and hosted. Needs both keys.
    FLPCR_GUARDIAN_ADDRESS the guardian: a multisig whose coin sends veto / rotate / resolve
                         messages (required in template mode)
    FLPCR_ADMIN_ADDRESS  the admin (default: the dev fee address, flpcr.networks)
    VITE_CFMM_POOL_DEV_FEE_ADDRESS_MAINNET  the mainnet dev fee address (flpcr.networks)
    FLPCR_LAUNCHERS      more launcher ids to host, comma-separated (optional): each takes
                         creates only if it passes the build pin, else reads and claims only
    FLPCR_DISCOVER       1 (default): also host every distributor launched for this host (hinted
                         with the build pin's hint on chain) that passes the build pin; 0: off
    FLPCR_DISCOVER_EVERY loop passes between discovery passes (default 5)
    FLPCR_NODE_URL       full node or coinset RPC (default: coinset for the network)
    FLPCR_FORGE_API      Forge router for pool -> LP asset ids, and for each CAT reward's terms
                         (/api/campaign-assets: the operator's switch and minimum, the Forge price
                         the $1 floor is set from) (default: the network's router)
    FLPCR_OPERATOR_SK    the operator's synthetic secret key, hex. Unset: the service serves
                         reads and claims but posts no roots.
    FLPCR_ROUTER_SK      the router's synthetic secret key, hex: a second key, separate from the
                         operator's, used only by the create intake to send "c" + id. Unset: the
                         intake quotes but refuses to create.
    FLPCR_ROUTER_RULES   JSON RouterRules (the owner's values): {"min_amount": {asset hex: mojos},
                         "min_per_day": {asset hex: mojos}, "max_live_per_creator": n,
                         "nft_collections": [DID hex, ...], "kinds": ["LP_HOLD", ...],
                         "min_claim_window": blocks, "max_start_lead": blocks,
                         "post_fee": mojos per root the creator prepays the operator}. A key left out
                         keeps its RouterRules() default: LP_HOLD only, 0.1 XCH or more, a claim
                         window of 7 days or more, a start within 30 days.
    FLPCR_POST_FEE       mojos per root post a create prepays the operator, over FLPCR_ROUTER_RULES'
                         post_fee (default 0.002 XCH). Testnet sets a token amount so test
                         campaigns stay cheap (owner, 2026-11-26); its operator pays the posts.
    FLPCR_LEASE_SECONDS  how long a create quote holds the singleton (default 120)
    FLPCR_CREATE_GATE    router: a creator must pass the Forge router's creation gate (the approved
                         wallets and NFT collections its Operator tab sets for pool deployment);
                         unset or off: anyone may create (owner, 2026-11-25: router while mainnet
                         is tested)
    FLPCR_EVERY          blocks between roots while campaigns run (default 180, ~1 h)
    FLPCR_LOOP_SECONDS   seconds between operator checks (default 60)
    FLPCR_CACHE_DIR      where the LP crawl cache and the published root manifests live (a
                         mounted volume; default ./data). Manifests: <dir>/manifests/<hash>.json,
                         served at GET /api/v1/manifests/<hash>
    FLPCR_WATCH          1: also run the watcher (flpcr.service.watcher) as an observer each loop,
                         alerting on a pending root that does not replay (default 0: off)
    FLPCR_WATCH_WEBHOOK  POST each watcher alert's JSON here (with FLPCR_WATCH=1)
    FLPCR_MANIFESTS      where the watcher reads root manifests: a folder or a URL template, e.g.
                         https://<host>/api/v1/manifests/{hash} (default: this service's own
                         <FLPCR_CACHE_DIR>/manifests)
    FLPCR_REGISTRY_DIR   folder holding <network>.json, the distributor registry served at
                         GET /api/v1/distributors (default: the repo's registry/)
    PORT                 HTTP port (default 8080)

Boot resyncs from chain; nothing else is needed on disk (a root's manifest is rebuilt
and written again if its file is missing). Each loop first calls `service.follow()`: a
spend of the singleton by anyone else (dispute, veto, rotation, a deploy script) is
picked up before the operator or the sequencer acts. The guardian and the admin
are multisigs and never live here. The host holds at most two keys: operator and router.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time
import traceback

from chia_wallet_sdk import SecretKey, standard_puzzle_hash

from ..build_pin import BuildPin
from ..networks import addresses, decode
from ..registry import pins_fingerprint

from ..chain.nfts import ChainNftSource, Combined
from ..live_source import LiveLpSource
from ..model import Kind
from .chain import NodeChain
from .core import OperatorKey
from .fleet import Fleet
from .http import serve
from .intake import cached, forge_cat_policies, forge_pool_infos, router_create_gate
from ..registry import Registry
from ..router import RouterRules
from .watcher import AlertSink, Watcher, manifest_lookup

NODES = {"mainnet": "https://api.coinset.org", "testnet11": "https://testnet11.api.coinset.org"}
ROUTERS = {"mainnet": "https://api.forge.awizard.dev", "testnet11": "https://testnet-api.forge.awizard.dev"}


def live_source_factory(network: str, node: str, api: str, cache_dir: pathlib.Path):
    """source_for(campaigns): every LP holder of the LP campaigns' pools, crawled from chain,
    and every holder of the NFT campaigns' collections (a col1 collection's DID and metadata
    id come from the campaign's own params, committed on chain)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    lp = LiveLpSource.connect(api, node, cache_dir / f"lp-crawl-{network}.json")
    lock = threading.Lock()

    def source_for(campaigns):
        with lock:
            pools = [c.target for c in campaigns if c.kind == Kind.LP_HOLD]
            if pools:
                lp.refresh([lp.lp_asset(t) for t in pools])
            collections = {c.target: c.params.collection for c in campaigns
                           if c.kind == Kind.NFT_HOLD and c.params.collection}
            from ..chain.node_reader import NodeReader
            return Combined(lp, ChainNftSource(NodeReader.connect(node), collections=collections))

    return source_for


def rules_from_env(text: str, post_fee: str = "") -> RouterRules:
    raw = json.loads(text) if text.strip() else {}
    if post_fee.strip():  # FLPCR_POST_FEE: the one rule an env line sets on its own
        raw["post_fee"] = int(post_fee)
    per_asset = lambda m: {bytes.fromhex(k.removeprefix("0x")): int(v) for k, v in (m or {}).items()}  # noqa: E731
    given = {}  # only what the owner set; everything else keeps its default
    if "min_amount" in raw:
        given["min_amount"] = per_asset(raw["min_amount"])
    if "min_per_day" in raw:
        given["min_per_day"] = per_asset(raw["min_per_day"])
    if "max_live_per_creator" in raw:
        given["max_live_per_creator"] = int(raw["max_live_per_creator"])
    if "nft_collections" in raw:
        given["nft_collections"] = frozenset(bytes.fromhex(x.removeprefix("0x")) for x in raw["nft_collections"] or [])
    if "kinds" in raw:
        given["kinds"] = frozenset(Kind[str(k).upper()] for k in raw["kinds"])
    if "min_claim_window" in raw:
        given["min_claim_window"] = int(raw["min_claim_window"])
    if "max_start_lead" in raw:
        given["max_start_lead"] = int(raw["max_start_lead"])
    if "post_fee" in raw:
        given["post_fee"] = int(raw["post_fee"])
        if given["post_fee"] < 0:
            raise ValueError("post_fee cannot be negative")
    return RouterRules(**given)


def template_from_env(network: str, env, operator, router_sk) -> BuildPin:
    """Template mode's build pin: the guardian (FLPCR_GUARDIAN_ADDRESS), the admin and dev fee
    address (flpcr.networks), this host's operator and router keys, production Config defaults."""
    if operator is None or router_sk is None:
        raise ValueError("needs FLPCR_OPERATOR_SK and FLPCR_ROUTER_SK: the build pin names both keys")
    guardian = env.get("FLPCR_GUARDIAN_ADDRESS", "").strip()
    if not guardian:
        raise ValueError("needs FLPCR_GUARDIAN_ADDRESS (a multisig)")
    roles = addresses(network, env)
    return BuildPin.template(network, operator_ph=operator.puzzle_hash,
                             router_ph=standard_puzzle_hash(router_sk.public_key()),
                             guardian_ph=decode(guardian, network), admin_ph=roles.admin_ph,
                             dev_fee_ph=roles.dev_fee_ph, pins=pins_fingerprint())


def main() -> int:
    env = os.environ
    network = env.get("FLPCR_NETWORK", "").strip()
    launcher = env.get("FLPCR_LAUNCHER", "").strip().removeprefix("0x")
    if network not in NODES or len(launcher) not in (0, 64):
        print("set FLPCR_NETWORK (testnet11|mainnet), and FLPCR_LAUNCHER (64 hex) or the template "
              "mode's FLPCR_GUARDIAN_ADDRESS", file=sys.stderr)
        return 2
    node = env.get("FLPCR_NODE_URL", "").strip() or NODES[network]
    api = env.get("FLPCR_FORGE_API", "").strip() or ROUTERS[network]
    sk_hex = env.get("FLPCR_OPERATOR_SK", "").strip().removeprefix("0x")
    operator = OperatorKey(SecretKey.from_bytes(bytes.fromhex(sk_hex))) if sk_hex else None
    every = int(env.get("FLPCR_EVERY", "180"))
    loop_seconds = int(env.get("FLPCR_LOOP_SECONDS", "60"))
    cache = pathlib.Path(env.get("FLPCR_CACHE_DIR", "data"))
    port = int(env.get("PORT", "8080"))

    chain = NodeChain(node)
    source_for = live_source_factory(network, node, api, cache)
    manifests_dir = cache / "manifests"
    router_hex = env.get("FLPCR_ROUTER_SK", "").strip().removeprefix("0x")
    router_sk = SecretKey.from_bytes(bytes.fromhex(router_hex)) if router_hex else None
    more = [bytes.fromhex(x.strip().removeprefix("0x")) for x in env.get("FLPCR_LAUNCHERS", "").split(",")
            if len(x.strip().removeprefix("0x")) == 64]
    template = None
    if not launcher:
        try:
            template = template_from_env(network, env, operator, router_sk)
        except ValueError as e:
            print(f"template mode: {e}", file=sys.stderr)
            return 2
    fleet = Fleet(chain, network, bytes.fromhex(launcher) if launcher else None, source_for, operator=operator,
                  router_sk=router_sk, template=template,
                  rules=rules_from_env(env.get("FLPCR_ROUTER_RULES", ""), env.get("FLPCR_POST_FEE", "")), pools=cached(lambda: forge_pool_infos(api)),
                  cat_policies=cached(lambda: forge_cat_policies(api), 120), every=every, manifests_dir=manifests_dir,
                  registry=Registry.load(network, env.get("FLPCR_REGISTRY_DIR", "").strip() or None),
                  configured=more, discover=env.get("FLPCR_DISCOVER", "1").strip() != "0",
                  create_gate=router_create_gate(api, network) if env.get("FLPCR_CREATE_GATE", "").strip() == "router" else None,
                  lease_seconds=float(env.get("FLPCR_LEASE_SECONDS", "120")))
    discover_every = max(1, int(env.get("FLPCR_DISCOVER_EVERY", "5")))
    watcher = sink = None
    if env.get("FLPCR_WATCH", "0").strip() == "1":  # an observer: the service holds no guardian key or bond
        manifests = manifest_lookup(env.get("FLPCR_MANIFESTS", "").strip() or str(manifests_dir))
        sink = AlertSink(env.get("FLPCR_WATCH_WEBHOOK", "").strip() or None)
        if launcher:
            watcher = Watcher(chain, bytes.fromhex(launcher), source_for, network=network, manifests=manifests)
        else:  # template mode: one watcher per hosted distributor, made as each is hosted
            fleet.watcher_factory = lambda lid: Watcher(chain, lid, source_for, network=network, manifests=manifests)
    server = serve(None, port, None, None, watcher, fleet=fleet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    where = f"primary distributor {launcher[:12]}…" if launcher else f"template mode (hint {template.hint.hex()[:12]}…)"
    print(f"flpcr-service: {network} {where}, node {node}, port {port}, "
          f"operator {'set (' + operator.puzzle_hash.hex()[:12] + '…)' if operator else 'not set: reads and claims only'}, "
          f"router key {'set' if router_sk else 'not set: quotes only'}, discovery {'on' if fleet.discover else 'off'}",
          flush=True)

    while not fleet.booted:  # boot: retried until the chain answers
        try:
            fleet.boot()
            for h in fleet.hosted.values():
                st = h.service.s
                print(f"hosting {h.launcher_id.hex()[:12]}… ({'XCH' if h.asset == bytes(32) else h.asset.hex()[:12] + '…'}): "
                      f"{st.resynced.generations} generations, {len(st.campaigns)} campaigns"
                      + ("" if h.build_pinned else f"; no creates ({h.why_not})"), flush=True)
            for lid, why in fleet.rejected.items():
                print(f"refused {lid.hex()[:12]}…: {why}", flush=True)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            time.sleep(30)

    passes = 0
    while True:
        passes += 1
        try:
            fleet.step(discover=passes % discover_every == 0, log=lambda m: print(m, flush=True))
            if watcher is not None:
                sink(watcher.check())
            if fleet.watcher_factory is not None:
                for h in list(fleet.hosted.values()):
                    if h.watcher is not None:
                        sink(h.watcher.check())
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(loop_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
