import sys, re
sys.path.insert(0, __import__('pathlib').Path(__file__).resolve().parents[4].as_posix())
import unittest, tests.test_chain as tc
from flpcr.chain.driver import Distributor
class T(tc.TestClaimRefusals):
    def runTest(self):
        w, c = self.w, self.camp
        w.claim(self.r, c.id, "alice")
        key = Distributor.claim_key(c.id, self.alice)
        saved = w.d.slots.pop(key)
        try:
            w.claim(self.r, c.id, "alice")
        except ValueError as e:
            print("refusal:", re.sub(r"0x[0-9a-f]+","",str(e))[:100])
t = T(); t.setUp(); t.runTest()
