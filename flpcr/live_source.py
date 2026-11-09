"""The engine's live `Source` for Forge LP campaigns: every holder of each
target pool's LP CAT, crawled from the chain (`lp_crawl`).

Every holder is in every root, whether or not they ever claim (the owner's
rule, 2026-10-20): what nobody claims by the end of the claim window goes back
to the creator through `reclaim`. A holder's share of an interval is their LP
over all non-custody LP held in it, time-weighted per block, so a position
held for a block or two earns next to nothing.

The pool -> LP asset map comes from the Forge responder (`lpCatAssetId`, which
`lp_crawl` re-derives as the eve puzzle hash's CAT), the coins from a full
node or coinset.org. Crawls are kept in a JSON cache so each root reads only
what is new.

Fee events come from each target pool's own spends (`v16_fees.PoolFeeWalk`):
one per `swap`, weighted by the LP fee it paid valued at the pool's pre-spend
spot price, exact. Only Forge V16 (protocol 17) pools are served: a pool the
responder lists at another protocol, or whose LP asset is not the V16 LP TAIL for
its launcher, is refused with `UnsupportedPool`. NFT events, links and
forwarders are not served here (NFTs come from `chain.nfts.Combined`).
"""

from __future__ import annotations

import json
import pathlib
import urllib.request
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Set, Tuple

from .links import Link
from .lp_crawl import CUSTODY, LpCrawl, Rpc, node_rpc
from .sources import HolderEvent, fee_weight
from .v16_fees import PoolFeeWalk, check_protocol


def parse_forge_pools(data: dict) -> Tuple[Dict[bytes, bytes], Dict[bytes, int]]:
    """(pool launcher id -> LP CAT asset id, pool launcher id -> protocol
    version) from a Forge responder's /api/pools answer (`poolVersion`)."""
    lp_assets, versions = {}, {}
    for entry in data.get("pools", []):
        pool = entry.get("pool") or {}
        lp = pool.get("lpCatAssetId") or (entry.get("state") or {}).get("lpCatAssetId")
        if pool.get("launcherCoinId") and lp:
            launcher = bytes.fromhex(pool["launcherCoinId"])
            lp_assets[launcher] = bytes.fromhex(lp)
            if pool.get("poolVersion") is not None:
                versions[launcher] = int(pool["poolVersion"])
    return lp_assets, versions


def fetch_forge_pools(api: str) -> Tuple[Dict[bytes, bytes], Dict[bytes, int]]:
    req = urllib.request.Request(f"{api.rstrip('/')}/api/pools", headers={"User-Agent": "flpcr-source/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return parse_forge_pools(json.loads(r.read()))


def forge_lp_assets(api: str) -> Dict[bytes, bytes]:
    """pool launcher id -> LP CAT asset id, from a Forge responder's /api/pools."""
    return fetch_forge_pools(api)[0]


@dataclass
class LiveLpSource:
    lp_assets: Dict[bytes, bytes]
    rpc: Rpc
    cache: Optional[pathlib.Path] = None
    crawls: Dict[bytes, LpCrawl] = field(default_factory=dict)
    # pool launcher id -> the protocol the responder lists it at (`poolVersion`)
    versions: Dict[bytes, int] = field(default_factory=dict)
    fee_walks: Dict[bytes, PoolFeeWalk] = field(default_factory=dict)
    _walked: Set[bytes] = field(default_factory=set, init=False, repr=False)  # fee walks brought to the tip in this process

    @classmethod
    def connect(cls, api: str, node: str, cache: Optional[pathlib.Path] = None) -> "LiveLpSource":
        lp_assets, versions = fetch_forge_pools(api)
        src = cls(lp_assets, node_rpc(node), cache, versions=versions)
        src.load_cache()
        return src

    def load_cache(self) -> None:
        if self.cache is None or not self.cache.exists():
            return
        data = json.loads(self.cache.read_text())
        if "lp" not in data and "fees" not in data:  # the first format: LP crawls only
            data = {"lp": data}
        for asset, crawl in data.get("lp", {}).items():
            self.crawls[bytes.fromhex(asset)] = LpCrawl.from_json(crawl)
        for pool, walk in data.get("fees", {}).items():
            self.fee_walks[bytes.fromhex(pool)] = PoolFeeWalk.from_json(walk)

    def save_cache(self) -> None:
        if self.cache is not None:
            self.cache.write_text(json.dumps({"lp": {k.hex(): v.to_json() for k, v in self.crawls.items()},
                                              "fees": {k.hex(): v.to_json() for k, v in self.fee_walks.items()}}))

    def refresh(self, assets) -> None:
        """Bring the crawl of each asset up to the chain's tip, then save the cache."""
        for asset in assets:
            crawl = self.crawls.setdefault(asset, LpCrawl(asset))
            crawl.update(self.rpc)
        self.save_cache()

    def refresh_fees(self, pools) -> None:
        """Bring the fee walk of each pool up to the chain's tip, then save the cache."""
        for pool in pools:
            self.lp_asset(pool)  # refuses anything but a V16 pool
            walk = self.fee_walks.setdefault(pool, PoolFeeWalk(pool))
            walk.update(self.rpc)
            self._walked.add(pool)
        self.save_cache()

    # --- Source ---------------------------------------------------------------------------

    def lp_asset(self, pool_launcher: bytes) -> bytes:
        """The pool's LP asset; refuses (`UnsupportedPool`) a pool that is not Forge V16."""
        if pool_launcher not in self.lp_assets:
            raise KeyError(f"pool {pool_launcher.hex()[:12]} is not a Forge pool the responder lists")
        lp = self.lp_assets[pool_launcher]
        check_protocol(pool_launcher, lp, self.versions.get(pool_launcher))
        return lp

    def holder_events(self, asset_id: bytes, before: int) -> List[HolderEvent]:
        if asset_id not in self.crawls:
            self.refresh([asset_id])
        return self.crawls[asset_id].events(before)

    def fee_events(self, pool_launcher: bytes, start: int, end: int) -> List[Tuple[int, Fraction]]:
        if pool_launcher not in self._walked:
            self.refresh_fees([pool_launcher])
        return sorted((h, fee_weight(w)) for h, w in self.fee_walks[pool_launcher].events(start, end))

    def nft_events(self, collection_id: bytes, before: int) -> List[HolderEvent]:
        return []

    def links(self) -> List[Link]:
        return []

    def excluded(self, asset_id: bytes) -> Set[bytes]:
        return set(CUSTODY)

    def forwarders(self, asset_id: bytes) -> Dict[bytes, bytes]:
        return {}
