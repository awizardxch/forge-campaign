"""FLPCR-01, -09, -17 and E-7 rerun: the engine probes (old E/e_engine.py,
E/e_float.py), unchanged in shape: a 50/50 time/fee campaign of 100,000 over
[0, 1000); an honest LP holds 1,000 throughout; the attacker adds 99,000 LP
at block 999 only and makes one weight-1 swap.

Fixes (flpcr/engine.py): each fee event pays by the holder's share averaged
over params.fee_window blocks (Weights.lookback_share), and the fee budget is
split in fixed campaign-relative buckets of fee_window blocks (fee_buckets),
so where roots are cut moves nothing; fee weights must be int or Fraction
(sources.fee_weight). Covered by tests/test_engine.py::TestFeeGaming,
TestCutIndependence, TestExactWeights, TestAccrual::test_blacklisted_wrapper_is_not_forwarded.

Old results: attack one root {honest 50450.5, attacker 49549.5}; vs honest
swap {75200.5, 24799.5}; cut at 998 {99851, 148}; floats crashed state_to_json;
a blacklisted wrapper's LP was forwarded to its holders.

Notes for the owner rules in CLAUDE.md:
  * params.fee_window has no floor above 1. A tiny window does not restore
    the attack: buckets shrink with the lookback, so a one-block position still
    earns about one block's share (checked below at 1 and 2).
  * the lookback includes the swap's own block (lookback_share averages the
    fee_window blocks ending at `height`, inclusive), so a deposit made in the
    swap's block counts for that one block (1/fee_window); it does not exclude
    the current block as the owner rule asks.
"""

from fractions import Fraction

from flpcr import Campaign, FixtureSource, HolderEvent as E, Kind, Params, accrue
from flpcr.engine import EngineState, run_epoch
from flpcr.manifest import state_to_json

from common import check, finish

POOL, LP = b"\xb0" * 32, b"\x1b" * 32
HON, ATK, VAULT, VSH = b"\xaa" * 32, b"\xee" * 32, b"\x77" * 32, b"\x5a" * 32
D = b"\xd1" * 32


def camp(**p):
    return Campaign(D, b"\xcc" * 32, b"\xca" * 32, 100_000, 0, 1000, Kind.LP_HOLD, POOL, Params(**p), fee_bps=0)


def show(credit):
    return {k.hex()[:4]: float(v) for k, v in sorted(credit.items())}


c = camp(time_bps=5000, fees_bps=5000)
quiet = FixtureSource(lp_assets={POOL: LP}, events={LP: [E(0, HON, 1000)]}, fees={POOL: []})
flash = FixtureSource(lp_assets={POOL: LP}, events={LP: [E(0, HON, 1000), E(999, ATK, 99_000)]}, fees={POOL: [(999, 1)]})
vs_honest = FixtureSource(lp_assets={POOL: LP}, events={LP: [E(0, HON, 1000), E(999, ATK, 99_000)]},
                          fees={POOL: [(500, 1000), (999, 1000)]})

print("\nFLPCR-01: flash deposit plus dust swap")
r = accrue(c, quiet, 0, 1000)
check("control (quiet pool) unchanged", r == {HON: 100_000}, str(show(r)))
r = accrue(c, flash, 0, 1000)
check("ATTACK one root [0,1000): attacker <= 1% (was 49549.5)", r[ATK] <= 1000, str(show(r)))
r = accrue(c, vs_honest, 0, 1000)
check("ATTACK vs an honest swap of equal fee: attacker <= 1% (was 24799.5)", r[ATK] <= 1000, str(show(r)))
for w in (1, 2, 191):
    try:
        camp(time_bps=5000, fees_bps=5000, fee_window=w)
        check(f"fee_window = {w} refused (floor 192, owner rule 2026-11-09)", False, "accepted")
    except ValueError as e:
        check(f"fee_window = {w} refused (floor 192, owner rule 2026-11-09)", True, str(e))
r1 = accrue(camp(time_bps=5000, fees_bps=5000, fee_window=192), flash, 0, 1000)
check("fee_window = 192 (the floor): a deposit in the swap's own block earns nothing of the fee half "
      "(the lookback is [h - W, h)); the attacker keeps only its one block of time share",
      r1[ATK] <= 50, str(show(r1)))

print("\nFLPCR-09: where roots are cut")
s = EngineState(D)
s, one = run_epoch(s, [c], flash, 1000)
s = EngineState(D)
s, _ = run_epoch(s, [c], flash, 998)
s, two = run_epoch(s, [c], flash, 1000)
cum = lambda root: {cl.puzzle_hash.hex()[:4]: cl.cumulative for cl in root.claims}  # noqa: E731
check("one root [0,1000) == roots [0,998)+[998,1000) (was {aaaa: 50450, eeee: 49549} vs {99851, 148})",
      cum(one) == cum(two), f"{cum(one)} vs {cum(two)}")

print("\nFLPCR-17: float fee weights")
for label, data in (("JSON float weight", {"fees": {POOL.hex(): [[100, 0.1]]}}),):
    try:
        FixtureSource.from_json(data)
        check(f"{label} refused at the Source boundary", False)
    except TypeError as e:
        check(f"{label} refused at the Source boundary", True, f"(TypeError: {e})")
fl = FixtureSource(lp_assets={POOL: LP}, events={LP: [E(0, HON, 3)]}, fees={POOL: [(100, 0.1), (600, 0.2)]})
try:
    accrue(c, fl, 0, 1000)
    check("in-memory float weight refused by accrue", False)
except TypeError:
    check("in-memory float weight refused by accrue", True)
ex = FixtureSource.from_json({"lp_assets": {POOL.hex(): LP.hex()}, "events": {LP.hex(): [[0, HON.hex(), 3], [500, ATK.hex(), 7]]},
                              "fees": {POOL.hex(): [[100, [1, 10]], [600, [1, 5]]]}})
s, _ = run_epoch(EngineState(D), [c], ex, 1000)
state_to_json(s)
check("control: rational weights stay exact and state_to_json works",
      all(isinstance(v, Fraction) for v in accrue(c, ex, 0, 1000).values()))

print("\nE-7: blacklisted wrapper and forwarding")
cb = camp(blacklist=frozenset({VAULT}))
w = FixtureSource(lp_assets={POOL: LP}, events={LP: [E(0, HON, 1000), E(0, VAULT, 1000)], VSH: [E(0, ATK, 5)]},
                  wrappers={LP: {VAULT: VSH}})
r = accrue(cb, w, 0, 1000)
check("blacklist={VAULT}, forward on: nothing reaches the vault's holders", ATK not in r and VAULT not in r, str(show(r)))
r = accrue(camp(), w, 0, 1000)
check("control: an unlisted vault forwards to its holders", ATK in r, str(show(r)))
finish("FLPCR-01/09/17, E-7")
