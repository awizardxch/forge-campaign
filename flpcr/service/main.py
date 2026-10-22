"""Run the campaign service: python -m flpcr.service.main

Environment (the host's service variables):

    FLPCR_NETWORK        testnet11 | mainnet                          (required)
    FLPCR_LAUNCHER       the distributor's launcher id, hex           (required)
    FLPCR_NODE_URL       full node or coinset RPC (default: coinset for the network)
    FLPCR_FORGE_API      Forge router for pool -> LP asset ids (default: the network's router)
    FLPCR_OPERATOR_SK    the operator's synthetic secret key, hex: the service's ONLY key.
                         Unset: the service serves reads and claims but posts no roots.
    FLPCR_EVERY          blocks between roots while campaigns run (default 180, ~1 h)
    FLPCR_LOOP_SECONDS   seconds between operator checks (default 60)
    FLPCR_CACHE_DIR      where the LP crawl cache lives (a mounted volume; default ./data)
    PORT                 HTTP port (default 8080)

Boot resyncs from chain; nothing else is needed on disk. The guardian and the admin
are multisigs and never live here.
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import time
import traceback

from chia_wallet_sdk import SecretKey

from ..chain.nfts import ChainNftSource, Combined
from ..live_source import LiveLpSource
from ..model import Kind
from .chain import NodeChain
from .core import CampaignService, OperatorKey
from .http import serve
from .sequencer import ClaimSequencer

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


def main() -> int:
    env = os.environ
    network = env.get("FLPCR_NETWORK", "").strip()
    launcher = env.get("FLPCR_LAUNCHER", "").strip().removeprefix("0x")
    if network not in NODES or len(launcher) != 64:
        print("set FLPCR_NETWORK (testnet11|mainnet) and FLPCR_LAUNCHER (64 hex)", file=sys.stderr)
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
    service = CampaignService(chain, bytes.fromhex(launcher), network, live_source_factory(network, node, api, cache),
                              operator=operator, every=every)
    sequencer = ClaimSequencer(service)
    server = serve(service, port, sequencer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"flpcr-service: {network} distributor {launcher[:12]}…, node {node}, port {port}, "
          f"operator {'set (' + operator.puzzle_hash.hex()[:12] + '…)' if operator else 'not set: reads and claims only'}",
          flush=True)

    while service.s is None:  # boot: retried until the chain answers
        try:
            st = service.boot()
            print(f"booted: {st.resynced.generations} generations, {len(st.campaigns)} campaigns, "
                  f"proofs through root {max(st.roots, default=0)}" + (f"; {st.mismatch}" if st.mismatch else ""), flush=True)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            time.sleep(30)

    while True:
        try:
            posted = service.operator_step()
            if posted:
                print(f"posted root {posted['root_id']} [{posted['start']}, {posted['end']}) fee {posted['fee']:,}", flush=True)
            paid = sequencer.run_batch()
            if paid:
                print(f"claims: {paid}", flush=True)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            try:  # resync: the record may be behind the chain (another builder spent the distributor)
                service.boot()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        time.sleep(loop_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
