from chia_wallet_sdk import Clvm, Simulator, Constants, NftMetadata, NftMint, TransferNft
from flpcr.chain.nfts import collection, ChainNftSource, owner_history
from flpcr.chain.sim import NftKit
c=Clvm(); sim=Simulator(); k=NftKit(sim,c)
minter, mallory, alice = k.wallet(), k.wallet(), k.wallet()
did=k.create_did(minter); WIZ=did.info.launcher_id
def meta(): return c.nft_metadata(NftMetadata(1,1,["x"],None,[],None,[],None))
# control: honest mint of 1 wizard
real, did = k.mint(minter, did, 1, to_ph=alice.puzzle_hash)
print("control collection == [real]:", collection(sim,c,WIZ)==[real[0].info.launcher_id])

# --- A: DID spend creates a 0-mojo coin at Mallory's address (e.g. a memo/notification). Mallory later
# spends it to launch her own NFT with NO DID involvement at all.
recreate = c.create_coin(did.info.inner_puzzle_hash(), 1, c.list([c.atom(minter.puzzle_hash)]))
gift = c.create_coin(mallory.puzzle_hash, 0, None)
did = c.spend_did(did, c.standard_spend(minter.pk, c.delegated_spend([recreate, gift])))
sim.spend_coins(c.coin_spends(), [minter.sk])
# find the gift coin
gift_coin = [s.coin for s in sim.children(did.coin.parent_coin_info) if s.coin.puzzle_hash==mallory.puzzle_hash][0]
# Mallory mints from the gift coin (launcher is grandchild of the DID coin), owner = nobody's DID
m = c.mint_nfts(gift_coin.coin_id(), [NftMint(meta(), Constants.nft_metadata_updater_default_hash(), mallory.puzzle_hash, mallory.puzzle_hash, 0, None)])
c.spend_standard_coin(gift_coin, mallory.pk, c.delegated_spend(m.parent_conditions))
fund = sim.new_coin(mallory.puzzle_hash, 1); c.spend_standard_coin(fund, mallory.pk, c.delegated_spend([]))
sim.spend_coins(c.coin_spends(), [mallory.sk])
fake = m.nfts[0].info.launcher_id
col = collection(sim,c,WIZ)
print("A: forged NFT counted as Wizard:", fake in col, " collection size", len(col))
ev = ChainNftSource(sim,c).nft_events(WIZ, sim.height()+1)
print("A: mallory wizard count:", sum(e.delta for e in ev if e.puzzle_hash==mallory.puzzle_hash))

# --- B: chia-wallet style single mint: launcher's parent is an XCH coin; DID only authorizes ownership
fund2 = sim.new_coin(minter.puzzle_hash, 1)
m2 = c.mint_nfts(fund2.coin_id(), [NftMint(meta(), Constants.nft_metadata_updater_default_hash(), alice.puzzle_hash, minter.puzzle_hash, 300,
                   TransferNft(did.info.launcher_id, [], did.info.inner_puzzle_hash()))])
c.spend_standard_coin(fund2, minter.pk, c.delegated_spend(m2.parent_conditions))
recreate = c.create_coin(did.info.inner_puzzle_hash(), 1, c.list([c.atom(minter.puzzle_hash)]))
did = c.spend_did(did, c.standard_spend(minter.pk, c.delegated_spend([recreate, c.create_puzzle_announcement(m2.nfts[0].info.launcher_id)])))
try:
    sim.spend_coins(c.coin_spends(), [minter.sk]); okB=True
except Exception as e:
    print("B: spend failed:", e); okB=False
if okB:
    nftB=m2.nfts[0]; print("B: DID-authorized single mint (launcher parent = XCH coin), owner DID set =", nftB.info.current_owner==WIZ,
      "; counted:", nftB.info.launcher_id in collection(sim,c,WIZ))
