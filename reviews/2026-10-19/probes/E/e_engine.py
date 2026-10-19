from fractions import Fraction as F
from flpcr import Campaign, FixtureSource, HolderEvent as E, Kind, Params, accrue
from flpcr.engine import EngineState, run_epoch
POOL, LP = b"\xb0"*32, b"\x1b"*32
HON, ATK, VAULT, VSH = b"\xaa"*32, b"\xee"*32, b"\x77"*32, b"\x5a"*32
D=b"\xd1"*32
def camp(**p): return Campaign(D, b"\xcc"*32, b"\xca"*32, 100_000, 0, 1000, Kind.LP_HOLD, POOL, Params(**p), fee_bps=0)
c = camp(time_bps=5000, fees_bps=5000)
# Honest LP holds 1000 for the whole campaign. Quiet pool: no swaps.
base = {LP:[E(0,HON,1000)]}
src0 = FixtureSource(lp_assets={POOL:LP}, events=dict(base), fees={POOL:[]})
print("control (quiet pool):", {k.hex()[:4]:float(v) for k,v in accrue(c,src0,0,1000).items()})
# Attack: at h=999 ATK flash-adds 99_000 LP, makes one dust swap (fee weight 1), leaves at 1000 (window end).
src1 = FixtureSource(lp_assets={POOL:LP}, events={LP:[E(0,HON,1000),E(999,ATK,99_000)]}, fees={POOL:[(999,1)]})
r = accrue(c,src1,0,1000); print("attack, one root [0,1000):", {k.hex()[:4]:float(v) for k,v in r.items()})
# Window-cut dependence: same chain, operator posts [0,998),[998,1000) instead
s=EngineState(D); s,_=run_epoch(s,[c],src1,998); s,root=run_epoch(s,[c],src1,1000)
print("same chain, roots cut at 998:", {k.hex()[:4]:v for k,v in ((cl.puzzle_hash,cl.cumulative) for cl in root.claims)})
# with a competing honest swap, attacker share of fee budget = w_a/(w_a+W) * 99%
src2 = FixtureSource(lp_assets={POOL:LP}, events={LP:[E(0,HON,1000),E(999,ATK,99_000)]}, fees={POOL:[(500,1000),(999,1000)]})
print("attack vs honest swap of equal fee:", {k.hex()[:4]:float(v) for k,v in accrue(c,src2,0,1000).items()})

# Blacklisting a vault wrapper is bypassed by forwarding
cb = camp(blacklist=frozenset({VAULT}))
src3 = FixtureSource(lp_assets={POOL:LP}, events={LP:[E(0,HON,1000),E(0,VAULT,1000)], VSH:[E(0,ATK,5)]}, wrappers={LP:{VAULT:VSH}})
print("blacklist={VAULT}, forward on:", {k.hex()[:4]:float(v) for k,v in accrue(cb,src3,0,1000).items()})
