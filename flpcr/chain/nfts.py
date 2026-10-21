"""NFT collections read from chain: which NFTs a DID minted, and who has held them.

A collection is the set of NFTs **minted by one DID**, named by the DID's
launcher id. Each NFT has its own launcher id. Only the DID's owner can make
a DID coin spend, so an NFT is counted only on evidence from one:

  1. **Created by a DID spend.** The NFT's launcher is a child of a DID
     spend, or the child of an NFT intermediate launcher (the standard
     `nft_intermediate_launcher` puzzle, checked by its revealed mod hash) that
     a DID spend created. Any other coin a DID spend creates (a payment, a
     memo or notification coin) belongs to someone else, who could launch
     anything from it, so it is never followed.
  2. **Announced by a DID spend at mint.** A DID spend announced the NFT's
     launcher id (a 32-byte puzzle announcement), and in the same block the
     NFT's eve spend set this DID as its owner. This is the reference wallet's
     DID-authorized mint, whose launcher's parent is an ordinary XCH coin.

The reader never trusts the NFT's owner field on its own (an NFT can be
transferred to a DID later without having been minted by it).

**Collections as wallets show them.** A DID can mint several collections; the
one an NFT belongs to is named in its CHIP-0007 metadata (`collection.id`), and
explorers name the collection `col1…` = sha256(DID launcher id in hex +
collection id) (checked against Spacescan on testnet11). A campaign that names
a `col1` id counts an NFT only when BOTH hold: the DID minted it (the on-chain
evidence above) and its metadata file, fetched and checked against the hash
the NFT carries on chain, names that collection id. Metadata the DID did not
mint means nothing (anyone can copy a collection id), and a file that is
missing or does not match its on-chain hash counts as not in the collection.

`ChainReader` is the three calls this needs. The simulator provides them
under these names; a full node or coinset.org provides the same data
(get_coin_record_by_name, get_coin_records_by_parent_ids,
get_puzzle_and_solution).
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Set, Tuple

from chia_wallet_sdk import Clvm, Coin, CoinSpend, CoinState, Constants, Did, Nft

from ..links import Link
from ..sources import HolderEvent, Source
from ..model import BURN_PUZZLE_HASH

# Enough for any DID spend; only used to read the spend's own conditions.
MAX_COST = 11_000_000_000


class ChainReader(Protocol):
    def coin_state(self, coin_id: bytes) -> Optional[CoinState]: ...

    def children(self, coin_id: bytes) -> List[CoinState]: ...

    def coin_spend(self, coin_id: bytes) -> Optional[CoinSpend]: ...


def _parse(clvm: Clvm, spend: CoinSpend):
    return clvm.deserialize(spend.puzzle_reveal).puzzle(), clvm.deserialize(spend.solution)


def did_coins(reader: ChainReader, clvm: Clvm, did_launcher_id: bytes) -> List[Coin]:
    """Every generation of the DID singleton, oldest first."""
    eve = [c for c in reader.children(did_launcher_id) if c.coin.amount % 2 == 1]
    if not eve:
        return []
    out = [eve[0].coin]
    coin = eve[0].coin
    while True:
        spend = reader.coin_spend(coin.coin_id())
        if spend is None:
            return out
        puzzle, solution = _parse(clvm, spend)
        nxt: Optional[Did] = None
        for child in reader.children(coin.coin_id()):
            if child.coin.amount % 2 == 1:
                did = puzzle.parse_child_did(coin, solution, child.coin)
                if did is not None and did.info.launcher_id == did_launcher_id:
                    nxt = did
                    break
        if nxt is None:  # melted, or not a DID at all
            return out
        coin = nxt.coin
        out.append(coin)


def _is_launcher(coin: Coin) -> bool:
    return coin.puzzle_hash == Constants.singleton_launcher_hash()


def _is_intermediate_launcher(reader: ChainReader, clvm: Clvm, coin: Coin) -> bool:
    """True if `coin` was spent as the standard NFT intermediate launcher
    (curried with the singleton launcher hash, a mint number and a total).
    The reveal hashes to the coin's puzzle hash by consensus, so checking the
    revealed mod is checking the puzzle hash."""
    spend = reader.coin_spend(coin.coin_id())
    if spend is None:
        return False
    try:
        curried = clvm.deserialize(spend.puzzle_reveal).uncurry()
    except Exception:
        return False
    return (
        curried is not None
        and curried.program.tree_hash() == Constants.nft_intermediate_launcher_hash()
        and len(curried.args) == 3
        and curried.args[0].to_atom() == Constants.singleton_launcher_hash()
    )


def _puzzle_announcements(clvm: Clvm, spend: CoinSpend) -> List[bytes]:
    """The messages of the CREATE_PUZZLE_ANNOUNCEMENTs a spend made."""
    out = clvm.deserialize(spend.puzzle_reveal).run(clvm.deserialize(spend.solution), MAX_COST, False)
    messages = []
    for condition in out.value.to_list():
        announcement = condition.parse_create_puzzle_announcement()
        if announcement is not None:
            messages.append(bytes(announcement.message))
    return messages


def _eve(reader: ChainReader, clvm: Clvm, launcher: Coin) -> Optional[Tuple[CoinState, Nft]]:
    """The NFT's eve coin and the NFT one step past it: the eve is spent at
    mint (to set the DID as owner), and parsing that spend yields an Nft whose
    launcher id we can check. None for a launcher of anything other than an NFT."""
    for eve in reader.children(launcher.coin_id()):
        if eve.coin.amount % 2 == 0:
            continue
        spend = reader.coin_spend(eve.coin.coin_id())
        if spend is None:
            return None
        nft = _child_nft(clvm, spend, eve.coin)
        if nft is not None and nft.info.launcher_id == launcher.coin_id():
            return eve, nft
    return None


def _child_nft(clvm: Clvm, spend: CoinSpend, coin: Coin) -> Optional[Nft]:
    try:
        puzzle, solution = _parse(clvm, spend)
        return puzzle.parse_child_nft(coin, solution)
    except Exception:
        return None  # not an NFT spend the SDK can read


def _first_generation(reader: ChainReader, clvm: Clvm, launcher: Coin) -> Optional[Nft]:
    found = _eve(reader, clvm, launcher)
    return found[1] if found is not None else None


def _announced_at_mint(reader: ChainReader, clvm: Clvm, launcher_id: bytes, did_launcher_id: bytes,
                       height: int) -> bool:
    """Path 2: the NFT with this launcher id had its eve spent at `height` (the
    DID spend's block) and the eve spend set the DID as owner."""
    state = reader.coin_state(launcher_id)
    if state is None or not _is_launcher(state.coin):
        return False
    found = _eve(reader, clvm, state.coin)
    if found is None:
        return False
    eve, nft = found
    return eve.spent_height == height and nft.info.current_owner == did_launcher_id


def collection(reader: ChainReader, clvm: Clvm, did_launcher_id: bytes) -> List[bytes]:
    """Launcher ids of every NFT the DID minted, in mint order."""
    found: List[bytes] = []
    seen: Set[bytes] = set()

    def add(launcher: Coin) -> None:
        cid = launcher.coin_id()
        if cid not in seen and _first_generation(reader, clvm, launcher) is not None:
            seen.add(cid)
            found.append(cid)

    for did_coin in did_coins(reader, clvm, did_launcher_id):
        spend = reader.coin_spend(did_coin.coin_id())
        if spend is None:
            continue  # the current generation: it has minted nothing yet
        # 1. Created by the DID spend, directly or through an intermediate launcher.
        for child in reader.children(did_coin.coin_id()):
            if _is_launcher(child.coin):
                add(child.coin)
            elif _is_intermediate_launcher(reader, clvm, child.coin):
                for grandchild in reader.children(child.coin.coin_id()):
                    if _is_launcher(grandchild.coin):
                        add(grandchild.coin)
        # 2. Announced by the DID spend, with the eve setting the DID as owner in the same block.
        spent_at = reader.coin_state(did_coin.coin_id()).spent_height
        for message in _puzzle_announcements(clvm, spend):
            if len(message) == 32 and message not in seen:
                if _announced_at_mint(reader, clvm, message, did_launcher_id, spent_at):
                    seen.add(message)
                    found.append(message)
    return found


def owner_history(reader: ChainReader, clvm: Clvm, launcher_id: bytes) -> List[Tuple[int, Optional[bytes]]]:
    """(height, owner puzzle hash) for every generation of one NFT, oldest
    first. The owner is the NFT's p2 puzzle hash, which is also its hint. If
    the NFT has ended (melted, or spent into something that does not parse as
    its next generation), a last entry (spent height, None) marks the end, so
    the last owner stops counting."""
    launcher = reader.coin_state(launcher_id)
    if launcher is None:
        return []
    nft = _first_generation(reader, clvm, launcher.coin)
    history: List[Tuple[int, Optional[bytes]]] = []
    while nft is not None:
        state = reader.coin_state(nft.coin.coin_id())
        if state is None:
            break
        history.append((state.created_height, nft.info.p2_puzzle_hash))
        if state.spent_height is None:
            break
        spend = reader.coin_spend(nft.coin.coin_id())
        nxt = _child_nft(clvm, spend, nft.coin) if spend is not None else None
        if nxt is None or nxt.info.launcher_id != launcher_id:
            history.append((state.spent_height, None))
            break
        nft = nxt
    return history


def collection_id(did_launcher_id: bytes, metadata_collection_id: str) -> bytes:
    """The `col1…` id explorers give a collection: sha256(DID hex + collection id)."""
    return hashlib.sha256((did_launcher_id.hex() + metadata_collection_id).encode()).digest()


Fetch = Callable[[str], Optional[bytes]]


# Public gateways for ipfs:// URIs (MintGarden Studio and most minting tools
# publish both an ipfs:// and a gateway URL). Any gateway will do: the file is
# accepted only if its sha256 equals the hash on chain.
IPFS_GATEWAYS = ("https://ipfs.io/ipfs/", "https://dweb.link/ipfs/")


def http_fetch(uri: str) -> Optional[bytes]:
    if uri.startswith("ipfs://"):
        for gateway in IPFS_GATEWAYS:
            body = http_fetch(gateway + uri[len("ipfs://"):])
            if body is not None:
                return body
        return None
    if not uri.startswith(("https://", "http://")):
        return None
    try:
        req = urllib.request.Request(uri, headers={"User-Agent": "flpcr-engine/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(2_000_000)
    except Exception:
        return None


def metadata_collection(nft: Nft, fetch: Fetch) -> Optional[str]:
    """The collection id an NFT's CHIP-0007 metadata names, from a file whose
    sha256 equals the metadata hash the NFT carries on chain. None when it has
    no metadata hash, no URI serves the matching file, or the file names none."""
    try:
        meta = nft.info.metadata.parse_nft_metadata()
    except Exception:
        return None
    if meta is None or not meta.metadata_hash:
        return None
    for uri in meta.metadata_uris:
        body = fetch(uri)
        if body is None or hashlib.sha256(body).digest() != meta.metadata_hash:
            continue
        try:
            doc = json.loads(body)
        except ValueError:
            return None
        coll = doc.get("collection") if isinstance(doc, dict) else None
        cid = (coll or {}).get("id") if isinstance(coll, dict) else None
        cid = cid or (doc.get("collection_id") if isinstance(doc, dict) else None)
        return cid if isinstance(cid, str) else None
    return None


class ChainNftSource:
    """`nft_events` for the engine, read from chain. `excluded` owners (offer
    settlement puzzles and the like) are never anyone's holding.

    A target is either a DID launcher id (every NFT the DID minted) or a `col1`
    collection id listed in `collections` as (DID launcher id, metadata
    collection id): then only the DID's NFTs whose verified metadata names
    that collection."""

    def __init__(self, reader: ChainReader, clvm: Optional[Clvm] = None, excluded: Iterable[bytes] = (),
                 collections: Optional[Dict[bytes, Tuple[bytes, str]]] = None, fetch: Fetch = http_fetch):
        self.reader = reader
        self.collections = dict(collections or {})
        for col, (did, uuid) in self.collections.items():
            if collection_id(did, uuid) != col:
                raise ValueError(f"{col.hex()[:12]} is not the collection id of DID {did.hex()[:12]} + {uuid}")
        self.fetch = fetch
        self._members: Dict[bytes, str] = {}  # launcher id -> collection id its metadata names ("" for none)
        self.clvm = clvm or Clvm()
        # Offer settlements hold an NFT mid-trade; the burn address ends it.
        self.excluded = set(excluded) | {Constants.settlement_payment_hash(), BURN_PUZZLE_HASH}

    def members(self, target: bytes) -> List[bytes]:
        """Launcher ids of the NFTs `target` names (a DID, or a listed `col1` id)."""
        if target not in self.collections:
            return collection(self.reader, self.clvm, target)
        did, uuid = self.collections[target]
        out = []
        for launcher_id in collection(self.reader, self.clvm, did):
            if launcher_id not in self._members:
                launcher = self.reader.coin_state(launcher_id)
                nft = _first_generation(self.reader, self.clvm, launcher.coin) if launcher else None
                self._members[launcher_id] = (metadata_collection(nft, self.fetch) or "") if nft else ""
            if self._members[launcher_id] == uuid:
                out.append(launcher_id)
        return out

    def nft_events(self, collection_id: bytes, before: int) -> List[HolderEvent]:
        events: List[HolderEvent] = []
        for launcher_id in self.members(collection_id):
            history = owner_history(self.reader, self.clvm, launcher_id)
            for i, (height, owner) in enumerate(history):
                if height >= before or owner is None:
                    break
                if owner not in self.excluded:
                    events.append(HolderEvent(height, owner, +1))
                    if i + 1 < len(history) and history[i + 1][0] < before:
                        events.append(HolderEvent(history[i + 1][0], owner, -1))
        return sorted(events, key=lambda e: e.height)


class Combined:
    """A `Source`: everything from `base`, except NFT events, read from chain."""

    def __init__(self, base: Source, nfts: ChainNftSource):
        self.base, self.nfts = base, nfts

    def lp_asset(self, pool_launcher: bytes) -> bytes:
        return self.base.lp_asset(pool_launcher)

    def holder_events(self, asset_id: bytes, before: int) -> List[HolderEvent]:
        return self.base.holder_events(asset_id, before)

    def fee_events(self, pool_launcher: bytes, start: int, end: int):
        return self.base.fee_events(pool_launcher, start, end)

    def nft_events(self, collection_id: bytes, before: int) -> List[HolderEvent]:
        return self.nfts.nft_events(collection_id, before)

    def links(self) -> List[Link]:
        return self.base.links()

    def excluded(self, asset_id: bytes) -> Set[bytes]:
        return self.base.excluded(asset_id)

    def forwarders(self, asset_id: bytes) -> Dict[bytes, bytes]:
        return self.base.forwarders(asset_id)
