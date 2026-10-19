from chia_wallet_sdk import SecretKey, standard_puzzle_hash, PublicKey, Signature
from flpcr import Campaign, FixtureSource, HolderEvent as E, Kind, Params, accrue
from flpcr.links import Link, LinkBook, sign, verify
from flpcr.model import Holding, Requirement
from flpcr.sources import FixtureSource as FS
POOL, LP, WIZ = b"\xb0"*32, b"\x1b"*32, b"\x33"*32
class A:
    def __init__(s,i): s.sk=SecretKey.from_seed(bytes([i])*32).derive_synthetic(); s.ph=standard_puzzle_hash(s.sk.public_key())
def link(p,m,h):
    mpk,ms=sign(m.sk,p.ph,m.ph); ppk,ps=sign(p.sk,p.ph,m.ph); return Link(p.ph,m.ph,h,mpk,ms,ppk,ps)
def unlink(m,h):
    pk,s=sign(m.sk,m.ph,m.ph); return Link(m.ph,m.ph,h,pk,s)
wiz, atk, other = A(1),A(2),A(3)
c = Campaign(b"\xd1"*32, b"\xcc"*32, b"\xca"*32, 1000, 100, 200, Kind.LP_HOLD, POOL, Params(gates=(Requirement(Holding.NFT,WIZ),)), fee_bps=0)
src=lambda links: FS(lp_assets={POOL:LP}, events={LP:[E(0,atk.ph,10),E(0,other.ph,10)]}, nfts={WIZ:[E(0,wiz.ph,1),E(0,other.ph,1)]}, address_links=links)
old = link(wiz, atk, 10)            # wiz once consented
# control: atk unlinks itself at 50 -> earns nothing in [100,200)
print("control (link@10, unlink@50):", {k.hex()[:4]:int(v) for k,v in accrue(c,src([old, unlink(atk,50)]),100,200).items()})
# 1) primary cannot revoke: no message type lets wiz remove atk. 2) replay: the same old signatures re-recorded at 150
replay = Link(old.primary, old.member, 150, old.member_pk, old.member_sig, old.primary_pk, old.primary_sig)
print("replayed old link @150:", {k.hex()[:4]:int(v) for k,v in accrue(c,src([old, unlink(atk,50), replay]),100,200).items()})
# 3) backdating: operator records a link signed 'now' at height 0 -- nothing signed binds the height
print("backdated link verify():", verify(Link(old.primary, old.member, 0, old.member_pk, old.member_sig, old.primary_pk, old.primary_sig)))
# 4) fixture/CLI path drops links entirely
print("FixtureSource.from_json keeps links:", hasattr(FS.from_json({"address_links":[1]}),'address_links') and FS.from_json({"address_links":[1]}).address_links!=[])
# 5) infinity key: standard_puzzle_hash(inf) address; sig identity
try:
    inf=PublicKey.infinity(); s0=Signature.infinity()
    ph=standard_puzzle_hash(inf)
    print("infinity-key unlink verifies:", verify(Link(ph,ph,0,inf.to_bytes(),s0.to_bytes())))
except Exception as e: print("inf:",e)
