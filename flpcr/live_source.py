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
what is new. Fee events, NFT events, links and forwarders are not served yet:
campaigns on this source are time-share only (params [10000, 0]).
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
from .sources import HolderEvent


def forge_lp_assets(api: str) -> Dict[bytes, bytes]:
    """pool launcher id -> LP CAT asset id, from a Forge responder's /api/pools."""
    req = urllib.request.Request(f"{api.rstrip('/')}/api/pools", headers={"User-Agent": "flpcr-source/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    out = {}
    for entry in data.get("pools", []):
        pool = entry.get("pool") or {}
        lp = pool.get("lpCatAssetId") or (entry.get("state") or {}).get("lpCatAssetId")
        if pool.get("launcherCoinId") and lp:
            out[bytes.fromhex(pool["launcherCoinId"])] = bytes.fromhex(lp)
    return out


@dataclass
class LiveLpSource:
    lp_assets: Dict[bytes, bytes]
    rpc: Rpc
    cache: Optional[pathlib.Path] = None
    crawls: Dict[bytes, LpCrawl] = field(default_factory=dict)

    @classmethod
    def connect(cls, api: str, node: str, cache: Optional[pathlib.Path] = None) -> "LiveLpSource":
        src = cls(forge_lp_assets(api), node_rpc(node), cache)
        if cache is not None and cache.exists():
            for asset, data in json.loads(cache.read_text()).items():
                src.crawls[bytes.fromhex(asset)] = LpCrawl.from_json(data)
        return src

    def refresh(self, assets) -> None:
        """Bring the crawl of each asset up to the chain's tip, then save the cache."""
        for asset in assets:
            crawl = self.crawls.setdefault(asset, LpCrawl(asset))
            crawl.update(self.rpc)
        if self.cache is not None:
            self.cache.write_text(json.dumps({k.hex(): v.to_json() for k, v in self.crawls.items()}))

    # --- Source ---------------------------------------------------------------------------

    def lp_asset(self, pool_launcher: bytes) -> bytes:
        if pool_launcher not in self.lp_assets:
            raise KeyError(f"pool {pool_launcher.hex()[:12]} is not a Forge pool the responder lists")
        return self.lp_assets[pool_launcher]

    def holder_events(self, asset_id: bytes, before: int) -> List[HolderEvent]:
        if asset_id not in self.crawls:
            self.refresh([asset_id])
        return self.crawls[asset_id].events(before)

    def fee_events(self, pool_launcher: bytes, start: int, end: int) -> List[Tuple[int, Fraction]]:
        return []

    def nft_events(self, collection_id: bytes, before: int) -> List[HolderEvent]:
        return []

    def links(self) -> List[Link]:
        return []

    def excluded(self, asset_id: bytes) -> Set[bytes]:
        return set(CUSTODY)

    def forwarders(self, asset_id: bytes) -> Dict[bytes, bytes]:
        return {}
