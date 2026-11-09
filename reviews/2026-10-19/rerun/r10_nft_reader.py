"""FLPCR-10 rerun (and the informational NFT melt gap): the collection reader
(old probes E/e_nft.py, E/e_nft_melt.py), the original code shapes on the
current flpcr/chain/nfts.py.

Fix: a launcher counts only on evidence from a DID spend (an intermediate
launcher the DID created, or a DID-announced launcher whose eve is owned by
the DID); an NFT the walk cannot follow, or melted, ends its owner's holding.
Covered by tests/test_nfts.py::TestCollectionEvidence, TestNftEnds.

Old results: A forged NFT counted True (collection size 2); B DID-authorized
mint from an XCH coin counted False; a melted NFT counted forever."""

from chia_wallet_sdk import Clvm, Constants, NftMetadata, NftMint, Simulator, TransferNft

from flpcr.chain.nfts import ChainNftSource, collection
from flpcr.chain.sim import NftKit
from flpcr.engine import Timeline

from common import check, finish

c = Clvm()
sim = Simulator()
k = NftKit(sim, c)
minter, mallory, alice = k.wallet(), k.wallet(), k.wallet()
did = k.create_did(minter)
WIZ = did.info.launcher_id


def meta():
    return c.nft_metadata(NftMetadata(1, 1, ["x"], None, [], None, [], None))


real, did = k.mint(minter, did, 1, to_ph=alice.puzzle_hash)
check("control: collection == [real]", collection(sim, c, WIZ) == [real[0].info.launcher_id])

# A: the DID's spend creates a 0-mojo coin at Mallory's address; she launches her own NFT from it.
recreate = c.create_coin(did.info.inner_puzzle_hash(), 1, c.list([c.atom(minter.puzzle_hash)]))
gift = c.create_coin(mallory.puzzle_hash, 0, None)
parent = did.coin.coin_id()
did = c.spend_did(did, c.standard_spend(minter.pk, c.delegated_spend([recreate, gift])))
sim.spend_coins(c.coin_spends(), [minter.sk])
gift_coin = [s.coin for s in sim.children(parent) if s.coin.puzzle_hash == mallory.puzzle_hash][0]
m = c.mint_nfts(gift_coin.coin_id(), [NftMint(meta(), Constants.nft_metadata_updater_default_hash(),
                                              mallory.puzzle_hash, mallory.puzzle_hash, 0, None)])
c.spend_standard_coin(gift_coin, mallory.pk, c.delegated_spend(m.parent_conditions))
fund = sim.new_coin(mallory.puzzle_hash, 1)
c.spend_standard_coin(fund, mallory.pk, c.delegated_spend([]))
sim.spend_coins(c.coin_spends(), [mallory.sk])
fake = m.nfts[0].info.launcher_id
col = collection(sim, c, WIZ)
check("ATTACK A: forged NFT under a DID payment is NOT counted (was True)", fake not in col, f"(size {len(col)})")
ev = ChainNftSource(sim, c).nft_events(WIZ, sim.height() + 1)
check("ATTACK A: mallory's wizard count is 0 (was 1)", sum(e.delta for e in ev if e.puzzle_hash == mallory.puzzle_hash) == 0)

# B: wallet-style single mint, launcher parent an XCH coin; the DID announces it and owns the eve.
fund2 = sim.new_coin(minter.puzzle_hash, 1)
m2 = c.mint_nfts(fund2.coin_id(), [NftMint(meta(), Constants.nft_metadata_updater_default_hash(), alice.puzzle_hash,
                                           minter.puzzle_hash, 300,
                                           TransferNft(did.info.launcher_id, [], did.info.inner_puzzle_hash()))])
c.spend_standard_coin(fund2, minter.pk, c.delegated_spend(m2.parent_conditions))
recreate = c.create_coin(did.info.inner_puzzle_hash(), 1, c.list([c.atom(minter.puzzle_hash)]))
did = c.spend_did(did, c.standard_spend(minter.pk, c.delegated_spend(
    [recreate, c.create_puzzle_announcement(m2.nfts[0].info.launcher_id)])))
sim.spend_coins(c.coin_spends(), [minter.sk])
check("B: DID-authorized single mint (launcher parent = XCH coin) is counted (was False)",
      m2.nfts[0].info.launcher_id in collection(sim, c, WIZ))

# Melt (informational gap). A real melt spend could not be built with the SDK's NFT helpers on the
# simulator (the ownership layer refuses a bare -113 CREATE_COIN: "path into atom"), as in the old
# probe. Stand-in, as tests/test_nfts.py::TestNftEnds does: a reader serving the NFT's spend with an
# unparseable solution, i.e. a spend the walk cannot follow, which is what a melt looks like to it.
from chia_wallet_sdk import CoinSpend  # noqa: E402


class Unreadable:
    def __init__(self, reader, coin_id):
        self.reader, self.coin_id = reader, coin_id

    def coin_state(self, coin_id):
        return self.reader.coin_state(coin_id)

    def children(self, coin_id):
        return self.reader.children(coin_id)

    def coin_spend(self, coin_id):
        spend = self.reader.coin_spend(coin_id)
        if coin_id != self.coin_id or spend is None:
            return spend
        return CoinSpend(spend.coin, spend.puzzle_reveal, b"\x80")


c2, sim2 = Clvm(), Simulator()
k2 = NftKit(sim2, c2)
minter2, alice2, bob2 = k2.wallet(), k2.wallet(), k2.wallet()
did2 = k2.create_did(minter2)
W2 = did2.info.launcher_id
(n1, n2), did2 = k2.mint(minter2, did2, 2, to_ph=alice2.puzzle_hash)
k2.transfer(alice2, n2, bob2.puzzle_hash)  # n2's spend becomes the unfollowable one
H = sim2.height() + 1000
tl = Timeline(ChainNftSource(Unreadable(sim2, n2.coin.coin_id()), c2).nft_events(W2, H))
counts = {("alice" if p == alice2.puzzle_hash else "bob"): v for p, v in tl.at(H - 1).items() if v}
check("melt stand-in: the NFT whose spend cannot be followed stops counting for alice (was: forever)",
      counts == {"alice": 1}, str(counts))
finish("FLPCR-10")
