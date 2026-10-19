from flpcr import Campaign, FixtureSource, HolderEvent as E, Kind, Params, accrue
from flpcr.engine import EngineState, run_epoch
from flpcr.manifest import state_to_json
POOL, LP, A, B = b"\xb0"*32, b"\x1b"*32, b"\xaa"*32, b"\xbb"*32
c = Campaign(b"\xd1"*32, b"\xcc"*32, b"\xca"*32, 10**12, 0, 1000, Kind.LP_HOLD, POOL, Params(time_bps=5000, fees_bps=5000), fee_bps=0)
src = FixtureSource.from_json({"lp_assets":{POOL.hex():LP.hex()},"events":{LP.hex():[[0,A.hex(),3],[500,B.hex(),7]]},"fees":{POOL.hex():[[100,0.1],[600,0.2]]}})
cr = accrue(c, src, 0, 1000); print({k.hex()[:4]:type(v).__name__ for k,v in cr.items()})
s,_ = run_epoch(EngineState(b"\xd1"*32),[c],src,1000)
try: state_to_json(s); print("state ok")
except Exception as e: print("state_to_json:", type(e).__name__, e)
