"""The watcher on the simulator: it replays the pending root from chain and the
service's Source, says MATCH, MISMATCH (with the diff) or UNVERIFIABLE, and
prepares (never signs) the dispute, veto or resolve its role may send."""

import json
import tempfile
import pathlib
import types
import unittest

try:
    from chia_wallet_sdk import SecretKey

    from flpcr.chain.driver import HEIGHT_WINDOW
    from flpcr.chain.sim import SimWorld
    from flpcr.manifest import manifest_hash
    from flpcr.service.chain import SimChain
    from flpcr.service.core import CampaignService, OperatorKey
    from flpcr.service.watcher import (MATCH, MISMATCH, NO_PENDING, UNVERIFIABLE, AlertSink, Watcher,
                                       manifest_lookup, publish_manifest)

    HAVE_CHAIN = True
except (ImportError, FileNotFoundError):  # pragma: no cover
    HAVE_CHAIN = False

from flpcr import EngineState, FixtureSource, HolderEvent, Kind, run_epoch

POOL, LP = b"\xb0" * 32, b"\x1b" * 32


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class WatcherTest(unittest.TestCase):
    def setUp(self):
        w = self.w = SimWorld()
        w.advance_to(w.h + 3)
        self.alice, self.bob, self.op = w.keys.ph("alice"), w.keys.ph("bob"), w.keys.ph("engine")
        self.src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, self.alice, 3), HolderEvent(0, self.bob, 1)]})
        start = w.h + HEIGHT_WINDOW + 2
        self.c = w.campaign("creator", 1_000_000, start, start + 40, Kind.LP_HOLD, POOL, claim_window=160)
        w.create(self.c, "creator")
        w.advance_to(self.c.start + 21)
        self.end = self.c.start + 20
        _, self.honest = run_epoch(self.fresh(), [self.c], self.src, self.end)
        # The operator's forgery: its own address holds a share it never held, so it is paid
        # from alice's and bob's emission. The totals stay inside the schedule.
        forged = FixtureSource(lp_assets={POOL: LP}, events={LP: self.src.events[LP] + [HolderEvent(0, self.op, 4)]})
        _, self.inflated = run_epoch(self.fresh(), [self.c], forged, self.end)
        self.manifests = pathlib.Path(tempfile.mkdtemp())

    def fresh(self):
        return EngineState(self.w.launcher_id, height=self.c.start)

    def post(self, root, with_manifest=False, publish=True):
        mh = manifest_hash(root) if with_manifest else bytes(32)
        if with_manifest and publish:
            publish_manifest(self.manifests, root)
        w = self.w
        w.push([w.d.post_root(w.h, root.root_id, root.start, root.end, root.root, sorted(root.totals.items()),
                              manifest_hash=mh)])

    def watcher(self, role="observer", **kw):
        if role == "disputer":
            kw.setdefault("disputer_ph", self.w.keys.ph("disputer"))
        kw.setdefault("manifests", manifest_lookup(str(self.manifests)))
        # One allocator: the test's driver applies the Prepared the watcher built.
        kw.setdefault("puzzles_factory", lambda: self.w.puzzles)
        return Watcher(SimChain(self.w.sim), self.w.launcher_id, kw.pop("source_for", lambda cs: self.src), role=role, **kw)

    def push_remedy(self, remedy):
        """Sign and push what the watcher prepared, as the role's wallet would."""
        w, p = self.w, remedy.prepared
        spends = list(p.spends)
        for m in p.messages:
            spends.extend(w.send(m.sender, m.message, m.mode))
        for ph, amount, memo in p.creations:
            spends.extend(w.create_from(remedy.disputer_coin, ph, amount, memo))
        w.sim.spend_coins(spends, [])
        w.d.apply(p)


class TestVerdicts(WatcherTest):
    def test_nothing_pending(self):
        self.assertEqual(self.watcher().check().status, NO_PENDING)

    def test_an_honest_root_matches(self):
        self.post(self.honest)
        r = self.watcher("guardian").check()
        self.assertEqual(r.status, MATCH, r.to_json())
        self.assertEqual(r.diff, [])
        self.assertIsNone(r.remedy)  # nothing to veto
        self.assertEqual(r.blocks_remaining, r.activates_at - self.w.h)
        self.assertFalse(r.should_alert)

    def test_an_honest_root_with_its_manifest_matches(self):
        self.post(self.honest, with_manifest=True)
        r = self.watcher("disputer").check()
        self.assertEqual(r.status, MATCH, r.to_json())
        self.assertEqual(r.holders, [])
        self.assertIsNone(r.remedy)

    def test_a_committed_manifest_nobody_published_is_unverifiable(self):
        self.post(self.honest, with_manifest=True, publish=False)
        r = self.watcher("disputer").check()
        self.assertEqual(r.status, UNVERIFIABLE)
        self.assertTrue(any("not published" in x for x in r.reasons))
        self.assertIsNone(r.remedy)  # never act on what was not checked
        self.assertTrue(r.should_alert)

    def test_no_manifest_when_one_is_required_is_unverifiable(self):
        self.post(self.honest)
        r = self.watcher(require_manifest=True).check()
        self.assertEqual(r.status, UNVERIFIABLE)

    def test_a_source_that_fails_is_unverifiable(self):
        self.post(self.honest)

        def broken(cs):
            raise ConnectionError("node unreachable")
        r = self.watcher("guardian", source_for=broken).check()
        self.assertEqual(r.status, UNVERIFIABLE)
        self.assertIn("node unreachable", r.reasons[0])
        self.assertIsNone(r.remedy)

    def test_a_forged_manifest_names_the_holders(self):
        self.post(self.inflated, with_manifest=True)
        r = self.watcher().check()
        self.assertEqual(r.status, MISMATCH)
        fields = {d["field"] for d in r.diff}
        self.assertIn("root", fields)
        self.assertIn("manifest_hash", fields)
        by = {h["puzzle_hash"]: h for h in r.holders}
        self.assertIsNone(by[self.op.hex()]["expected"])  # paid, never held
        self.assertGreater(by[self.op.hex()]["posted"], 0)
        self.assertLess(by[self.alice.hex()]["posted"], by[self.alice.hex()]["expected"])
        alert = r.alert()
        json.dumps(alert)  # webhook-ready
        self.assertEqual(alert["status"], MISMATCH)

    def test_a_tampered_published_file_is_caught(self):
        self.post(self.honest, with_manifest=True)
        path = self.manifests / f"{manifest_hash(self.honest).hex()}.json"
        data = json.loads(path.read_text())
        data["claims"][0]["cumulative"] += 1
        path.write_text(json.dumps(data))
        r = self.watcher().check()
        self.assertEqual(r.status, MISMATCH)
        self.assertIn("manifest.file_hash", {d["field"] for d in r.diff})


class TestRemedies(WatcherTest):
    def test_a_disputer_watcher_disputes_and_the_root_does_not_activate(self):
        w = self.w
        self.post(self.inflated)
        bond = w.cfg.dispute_bond
        w.sim.new_coin(w.keys.ph("disputer"), bond + 1)
        r = self.watcher("disputer").check()
        self.assertEqual(r.status, MISMATCH)
        self.assertIn("root", {d["field"] for d in r.diff})
        self.assertIsNone(r.holders)  # no manifest: the posted leaves are unknown
        self.assertEqual(r.remedy.action, "dispute")
        self.assertEqual(r.remedy.prepared.creations, [(w.d.bond_launcher_ph(w.keys.ph("disputer")), bond, w.launcher_id)])
        self.assertEqual(r.remedy.to_json()["creations"][0]["from_coin"], r.remedy.disputer_coin.coin_id().hex())
        self.assertGreater(r.blocks_remaining, 0)
        self.push_remedy(r.remedy)
        st = w.state()
        self.assertEqual(st.dispute.root_id, 1)
        w.advance_to(st.pending.activates_at + 2)
        again = self.watcher("disputer").check()
        self.assertIsNotNone(again.dispute)
        self.assertIsNone(again.remedy)  # already disputed: the guardian settles it
        # The guardian's watcher resolves it upheld: the root is revoked, the bond refunded.
        before = w.xch_balance("disputer")
        g = self.watcher("guardian").check()
        self.assertEqual((g.remedy.action, g.remedy.upheld), ("resolve", 1))
        self.push_remedy(g.remedy)
        st = w.state()
        self.assertIsNone(st.pending)
        self.assertIsNone(st.active)  # never activated
        self.assertEqual(w.xch_balance("disputer"), before + bond)

    def test_the_guardian_rejects_a_dispute_on_an_honest_root(self):
        w = self.w
        self.post(self.honest)
        w.dispute(1)
        g = self.watcher("guardian").check()
        self.assertEqual(g.status, MATCH)
        self.assertEqual((g.remedy.action, g.remedy.upheld), ("resolve", 0))
        self.push_remedy(g.remedy)
        self.assertIsNone(w.state().dispute)
        self.assertIsNotNone(w.state().pending)

    def test_the_guardian_vetoes(self):
        w = self.w
        self.post(self.inflated)
        r = self.watcher("guardian").check()
        self.assertEqual(r.remedy.action, "veto")
        self.assertEqual([m.sender for m in r.remedy.prepared.messages], [w.cfg.guardian_ph])
        self.push_remedy(r.remedy)
        self.assertIsNone(w.state().pending)
        # The operator reposts the honest root under the same id, and it replays.
        self.post(self.honest)
        self.assertEqual(self.watcher("guardian").check().status, MATCH)

    def test_too_late_prepares_nothing(self):
        w = self.w
        self.post(self.inflated)
        w.advance_to(w.state().pending.activates_at + 1)
        r = self.watcher("guardian").check()
        self.assertEqual(r.status, MISMATCH)
        self.assertLessEqual(r.blocks_remaining, 0)
        self.assertIsNone(r.remedy.prepared)
        self.assertIn("too late", r.remedy.refused)

    def test_an_unfunded_disputer_is_told_to_fund(self):
        self.post(self.inflated)
        r = self.watcher("disputer").check()
        self.assertIsNone(r.remedy.prepared)
        self.assertIn("fund it", r.remedy.refused)

    def test_alerts_once(self):
        self.post(self.inflated)
        sent = []
        sink = AlertSink(out=types.SimpleNamespace(write=sent.append, flush=lambda: None))
        watcher = self.watcher()
        self.assertTrue(sink(watcher.check()))
        self.assertFalse(sink(watcher.check()))


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestServiceAfterAVeto(unittest.TestCase):
    def test_boot_skips_a_vetoed_root(self):
        """A vetoed root stays among the post_root spends; the service must not build on it."""
        sk = SecretKey.from_seed(bytes([7]) * 32).derive_synthetic()
        op = OperatorKey(sk)
        w = SimWorld(engine_ph=op.puzzle_hash)
        w.wallets[op.puzzle_hash] = types.SimpleNamespace(sk=sk, pk=sk.public_key(), puzzle_hash=op.puzzle_hash)
        w.sim.new_coin(op.puzzle_hash, 10**13)
        w.advance_to(w.h + 3)
        src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, w.keys.ph("alice"), 1)]})
        start = w.h + HEIGHT_WINDOW + 2
        c = w.campaign("creator", 1_000_000, start, start + 40, Kind.LP_HOLD, POOL, claim_window=160)
        w.create(c, "creator")
        svc = CampaignService(SimChain(w.sim), w.launcher_id, "testnet11", lambda cs: src, operator=op, every=10)
        svc.boot()
        w.d = svc.d
        w.advance_to(c.start + 12)
        self.assertEqual(svc.operator_step(wait=5, poll=0)["root_id"], 1)
        w.veto(1)
        svc.boot()
        self.assertEqual(sorted(svc.s.roots), [])
        self.assertIsNone(svc.s.mismatch)
        w.d = svc.d
        self.assertEqual(svc.operator_step(wait=5, poll=0)["root_id"], 1)  # reposted under the same id


if __name__ == "__main__":
    unittest.main()
