from chia_wallet_sdk import Clvm, Simulator
from flpcr.chain.nfts import ChainNftSource
from flpcr.chain.sim import NftKit
from flpcr.engine import Timeline
c=Clvm(); sim=Simulator(); k=NftKit(sim,c)
minter, alice, bob = k.wallet(), k.wallet(), k.wallet()
did=k.create_did(minter); WIZ=did.info.launcher_id
(n1,n2), did = k.mint(minter, did, 2, to_ph=alice.puzzle_hash)
# control: honest transfer of n1 to bob decrements alice
k.transfer(alice, n1, bob.puzzle_hash)
# attack: alice melts n2 (singleton melt: CREATE_COIN amount -113). The NFT no longer exists.
try:
    c.spend_nft(n2, c.standard_spend(alice.pk, c.delegated_spend([c.create_coin(alice.puzzle_hash, -113, c.list([c.atom(alice.puzzle_hash)]))])))
except Exception as e:
    print("spend_nft builder:", e)
sim.spend_coins(c.coin_spends(), [alice.sk])
print("n2 coin spent:", sim.coin_state(n2.coin.coin_id()).spent_height is not None,
      "; children:", [ (s.coin.amount) for s in sim.children(n2.coin.coin_id())])
H=sim.height()+1000
tl=Timeline(ChainNftSource(sim,c).nft_events(WIZ, H))
print("counts long after melt:", {('alice' if p==alice.puzzle_hash else 'bob'):v for p,v in tl.at(H-1).items()})
