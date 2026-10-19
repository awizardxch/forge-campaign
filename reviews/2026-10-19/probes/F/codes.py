"""Log the refusal message behind every refused() call in tests/test_chain.py."""
import sys, unittest, re, collections
sys.path.insert(0, __import__('pathlib').Path(__file__).resolve().parents[4].as_posix())
import tests.test_chain as tc
orig = tc.refused
log = collections.defaultdict(list)
def refused(test, fn, *a, **k):
    try:
        fn(*a, **k)
    except ValueError as e:
        m = str(e)
        m = re.sub(r"0x[0-9a-f]+", "", m)[:90]
        log[test.id().split(".", 2)[-1]].append(m)
        test.assertTrue("clvm raise" in m or "Simulator error" in m, m)
        return
    test.fail("not refused")
tc.refused = refused
suite = unittest.defaultTestLoader.loadTestsFromModule(tc)
unittest.TextTestRunner(verbosity=0).run(suite)
for t, ms in sorted(log.items()):
    if "Cat." in t: continue
    print(t, "|", " ; ".join(ms))
