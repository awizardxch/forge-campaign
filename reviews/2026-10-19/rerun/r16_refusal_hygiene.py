"""FLPCR-16 rerun: probes that passed for the wrong reason, and a refusal
helper that pinned nothing (old probes F/pinprobe.py, F/firstclaim.py,
F/codes.py).

1. The height-pin probe: now authorized by the creator (TestHeightPin::
   test_an_early_reclaim_needs_a_future_h), pinned AssertHeightAbsoluteFailed,
   and shown ACCEPTED on the mutant without the pin: see r05_height_pin.py.
2. The first-claim probe (TestClaimRefusals::test_first_claim_cannot_insert_a_second_slot):
   a whale leaves room under the posted total, so the old first refusal
   (posted-total check) cannot fire; the refusal is now consensus
   MessageNotSentOrReceived (the forged neighbours' slots do not exist), not
   the old CoinAmountNegative. Rerun here in the original shape.
3. refused(): tests/test_chain.py's helper accepts only `clvm raise` or a
   `Simulator error`, and `expect=` pins which. Counted here.
4. AGENT_TOOLS rule 5 now says a puzzle refusal is a bare `clvm raise` with
   no rule named (docs/AGENT_TOOLS.md line 32)."""

import pathlib
import re
import unittest

from flpcr import EngineState, Kind, run_epoch

from common import ASSETS, HEIGHT_WINDOW, LEAD, POOL, Distributor, attempt, check, finish, src, world

ROOT = pathlib.Path(__file__).resolve().parents[3]

for tag, asset in ASSETS:
    print(f"\n{tag}")
    w, _ = world(asset)
    c = w.campaign("creator", 1_000_000, w.h + LEAD + 2, w.h + LEAD + 42, Kind.LP_HOLD, POOL, claim_window=56)
    w.create(c, "creator")
    alice = w.keys.ph("alice")
    w.advance_to(c.end + 1)
    _, r = run_epoch(EngineState(w.launcher_id, height=c.start), [c], src((alice, 1), (w.keys.ph("whale"), 1000)), c.end)
    w.post(r)
    w.advance_to(w.h + HEIGHT_WINDOW + w.cfg.dispute_blocks)
    attempt("control: alice's first claim", w.claim, r, c.id, "alice")
    key = Distributor.claim_key(c.id, alice)
    saved = w.d.claims.pop(key)  # the driver forgets alice's slot: it builds an insert between her neighbours
    attempt("ATTACK a second first-claim insert for alice (old: CoinAmountNegative / posted-total)", w.claim, r, c.id,
            "alice", expect="refuse", reason="MessageNotSentOrReceived")
    w.d.claims[key] = saved

print("\nrefused() in tests/test_chain.py")
text = (ROOT / "tests/test_chain.py").read_text()
calls = re.findall(r"refused\((?:[^()]|\([^()]*\))*\)", text, re.S)
pinned = [x for x in calls if "expect=" in x]
check(f"refused() calls pinning a reason with expect=: {len(pinned)} of {len(calls)}", len(pinned) >= 0.8 * len(calls))
import tests.test_chain as tc  # noqa: E402


class Probe(unittest.TestCase):
    def runTest(self):
        pass


t = Probe()
try:
    tc.refused(t, lambda: (_ for _ in ()).throw(TypeError("a driver bug")))
    check("refused() treats a Python error as a failure, not a refusal", False)
except TypeError:
    check("refused() treats a Python error as a failure, not a refusal", True)
try:
    tc.refused(t, lambda: (_ for _ in ()).throw(ValueError("Simulator error: DoubleSpend")), expect="clvm raise")
    check("refused(expect=...) fails on a refusal for another reason", False)
except AssertionError:
    check("refused(expect=...) fails on a refusal for another reason", True)
rule5 = (ROOT / "docs/AGENT_TOOLS.md").read_text()
check("AGENT_TOOLS no longer says a clvm raise names a puzzle rule", "with no rule named" in rule5)
finish("FLPCR-16")
