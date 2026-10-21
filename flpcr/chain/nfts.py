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

The reader never trusts the off-chain "collection id" in CHIP-0007 metadata,
or the NFT's owner field on its own (an NFT can be transferred to a DID later
without having been minted by it).

`ChainReader` is the three calls this needs. The simulator provides them
under these names; a full node or coinset.org provides the same data
(get_coin_record_by_name, get_coin_records_by_parent_ids,
get_puzzle_and_solution).
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Protocol, Set, Tuple

from chia_wallet_sdk import Clvm, Coin, CoinSpend, CoinState, Constants, Did, Nft

from ..links import Link
from ..sources import HolderEvent, Source

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
