"""NFT collections read from chain: which NFTs a DID minted, and who has held them.

A collection is the set of NFTs **minted by one DID**, named by the DID's
launcher id. Each NFT has its own launcher id, and only the DID's owner can
mint into the collection: the NFT launchers are created by a spend of the
DID singleton itself (directly, or through an intermediate launcher), and
nothing else can make a DID coin spend. That is why this module discovers a
collection by walking the DID's own spends, and never trusts the off-chain
"collection id" in CHIP-0007 metadata, or the NFT's owner field (an NFT
can be transferred to a DID later without having been minted by it).

`ChainReader` is the three calls this needs. The simulator provides them
under these names; a full node or coinset.org provides the same data
(get_coin_record_by_name, get_coin_records_by_parent_ids,
get_puzzle_and_solution).
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Protocol, Set

from chia_wallet_sdk import Clvm, Coin, CoinSpend, CoinState, Constants, Did, Nft

from ..sources import HolderEvent

# How far below a DID spend an NFT launcher may sit: direct child, or the
# child of an intermediate launcher the DID spend created.
LAUNCHER_DEPTH = 2


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


def collection(reader: ChainReader, clvm: Clvm, did_launcher_id: bytes) -> List[bytes]:
    """Launcher ids of every NFT the DID minted, in mint order."""
    found: List[bytes] = []
    seen: Set[bytes] = set()
    for did_coin in did_coins(reader, clvm, did_launcher_id):
        frontier = [did_coin.coin_id()]
        for _ in range(LAUNCHER_DEPTH):
            nxt = []
            for parent_id in frontier:
                for child in reader.children(parent_id):
                    cid = child.coin.coin_id()
                    if child.coin.puzzle_hash == Constants.singleton_launcher_hash():
                        if cid not in seen and _first_generation(reader, clvm, child.coin) is not None:
                            seen.add(cid)
                            found.append(cid)
                    elif child.coin.puzzle_hash != did_coin.puzzle_hash:
                        nxt.append(cid)  # maybe an intermediate launcher
            frontier = nxt
    return found


def _first_generation(reader: ChainReader, clvm: Clvm, launcher: Coin) -> Optional[Nft]:
    """The NFT one step past its eve coin: the eve is spent at mint (to set the
    DID as owner), and parsing that spend yields an Nft whose launcher id we
    can check. None for a launcher of anything other than an NFT."""
    for eve in reader.children(launcher.coin_id()):
        if eve.coin.amount % 2 == 0:
            continue
        spend = reader.coin_spend(eve.coin.coin_id())
        if spend is None:
            return None
        puzzle, solution = _parse(clvm, spend)
        nft = puzzle.parse_child_nft(eve.coin, solution)
        if nft is not None and nft.info.launcher_id == launcher.coin_id():
            return nft
    return None


def owner_history(reader: ChainReader, clvm: Clvm, launcher_id: bytes) -> List[tuple]:
    """(created_height, owner puzzle hash) for every generation of one NFT,
    oldest first. The owner is the NFT's p2 puzzle hash, which is also its hint."""
    launcher = reader.coin_state(launcher_id)
    if launcher is None:
        return []
    nft = _first_generation(reader, clvm, launcher.coin)
    history = []
    while nft is not None:
        state = reader.coin_state(nft.coin.coin_id())
        if state is None:
            break
        history.append((state.created_height, nft.info.p2_puzzle_hash))
        spend = reader.coin_spend(nft.coin.coin_id())
        if spend is None:
            break
        puzzle, solution = _parse(clvm, spend)
        nft = puzzle.parse_child_nft(nft.coin, solution)
    return history


class ChainNftSource:
    """`nft_events` for the engine, read from chain. `excluded` owners (offer
    settlement puzzles and the like) are never anyone's holding."""

    def __init__(self, reader: ChainReader, clvm: Optional[Clvm] = None, excluded: Iterable[bytes] = ()):
        self.reader = reader
        self.clvm = clvm or Clvm()
        self.excluded = set(excluded) | {Constants.settlement_payment_hash()}

    def nft_events(self, collection_id: bytes, before: int) -> List[HolderEvent]:
        events: List[HolderEvent] = []
        for launcher_id in collection(self.reader, self.clvm, collection_id):
            history = owner_history(self.reader, self.clvm, launcher_id)
            for i, (height, owner) in enumerate(history):
                if height >= before:
                    break
                if owner not in self.excluded:
                    events.append(HolderEvent(height, owner, +1))
                    if i + 1 < len(history) and history[i + 1][0] < before:
                        events.append(HolderEvent(history[i + 1][0], owner, -1))
        return sorted(events, key=lambda e: e.height)


class Combined:
    """A `Source` built from a base source with NFT events taken from chain."""

    def __init__(self, base, nfts: ChainNftSource):
        self.base, self.nfts = base, nfts

    def __getattr__(self, name):
        return getattr(self.base, name)

    def nft_events(self, collection_id: bytes, before: int) -> List[HolderEvent]:
        return self.nfts.nft_events(collection_id, before)
