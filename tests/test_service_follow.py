"""The service keeps up with the chain and publishes what it posts.

* Manifests: the operator commits `manifest_hash(root)` and publishes the manifest; a
  watcher reading it from the service's GET /api/v1/manifests/<hash> checks every holder's
  leaf, and catches a tampered file.
* Foreign spends: a dispute, veto, rotation or create by someone else is noticed on the
  service's next check (`follow`), before it builds anything on a stale ChainState.
* Boot replays each root over every campaign live when it was posted, so a root that
  silently dropped one is a mismatch, and no proof past it is served.
* Boot with no campaign leaves the engine's start open (not height 0).
"""

import json
import pathlib
import tempfile
import threading
import types
import unittest

try:
    from chia_wallet_sdk import SecretKey

    from flpcr.chain.driver import HEIGHT_WINDOW
    from flpcr.chain.resync import resync
    from flpcr.chain.sim import SimWorld
    from flpcr.manifest import manifest_hash
    from flpcr.service.chain import SimChain
    from flpcr.service.core import CampaignService, OperatorKey
    from flpcr.service.http import serve
    from flpcr.service.watcher import MATCH, MISMATCH, Watcher, manifest_lookup

    HAVE_CHAIN = True
except (ImportError, FileNotFoundError):  # pragma: no cover
    HAVE_CHAIN = False

from flpcr import EngineState, FixtureSource, HolderEvent, Kind, run_epoch

POOL, LP = b"\xb0" * 32, b"\x1b" * 32
POOL2, LP2 = b"\xb2" * 32, b"\x2b" * 32
WINDOW = 56


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class ServiceTest(unittest.TestCase):
    create_campaign = True

    def setUp(self):
        sk = SecretKey.from_seed(bytes([7]) * 32).derive_synthetic()
        self.op = OperatorKey(sk)
        w = self.w = SimWorld(engine_ph=self.op.puzzle_hash)
        w.wallets[self.op.puzzle_hash] = types.SimpleNamespace(sk=sk, pk=sk.public_key(), puzzle_hash=self.op.puzzle_hash)
        w.sim.new_coin(self.op.puzzle_hash, 10**13)
        w.advance_to(w.h + 3)
        self.alice, self.bob = w.keys.ph("alice"), w.keys.ph("bob")
        self.src = FixtureSource(lp_assets={POOL: LP, POOL2: LP2},
                                 events={LP: [HolderEvent(0, self.alice, 3), HolderEvent(0, self.bob, 1)],
                                         LP2: [HolderEvent(0, self.bob, 2)]})
        self.manifests = pathlib.Path(tempfile.mkdtemp())
        if self.create_campaign:
            start = w.h + HEIGHT_WINDOW + 2
            self.c = w.campaign("creator", 1_000_000, start, start + 40, Kind.LP_HOLD, POOL, claim_window=WINDOW)
            w.create(self.c, "creator")

    def service(self, **kw):
        svc = CampaignService(SimChain(self.w.sim), self.w.launcher_id, "testnet11", lambda cs: self.src,
                              operator=self.op, every=kw.pop("every", 10), manifests_dir=self.manifests, **kw)
        svc.boot()
        return svc

    def world_follows(self):
        """The test's own builder (someone else's, to the service): its driver rebuilt from chain."""
        self.w.d = resync(self.w.sim, self.w.puzzles, self.w.launcher_id).distributor

    def op_balance(self):
        return self.w.xch_balance(self.op.puzzle_hash)


class TestManifests(ServiceTest):
    def test_the_operator_commits_and_publishes_its_manifest(self):
        w = self.w
        svc = self.service()
        w.advance_to(self.c.start + 12)
        posted = svc.operator_step(wait=5, poll=0)
        root = svc.s.roots[1]
        mh = manifest_hash(root).hex()
        self.assertEqual(posted["manifest_hash"], mh)
        self.assertTrue((self.manifests / f"{mh}.json").is_file())
        self.assertEqual(svc.status()["pending"]["manifest_hash"], mh)
        self.assertEqual(svc.manifest(mh)["root"], root.root.hex())
        self.assertEqual(svc.manifest(mh + ".json")["root"], root.root.hex())
        self.assertIsNone(svc.manifest("ab" * 32))
        with self.assertRaises(ValueError):
            svc.manifest("not-a-hash")
        # The chain carries it: a restarted service reads it back, and rewrites a lost file.
        (self.manifests / f"{mh}.json").unlink()
        again = self.service()
        self.assertIsNone(again.s.mismatch)
        self.assertEqual(again.status()["pending"]["manifest_hash"], mh)
        self.assertTrue((self.manifests / f"{mh}.json").is_file())

    def test_a_watcher_checks_holders_through_the_service(self):
        w = self.w
        svc = self.service()
        w.advance_to(self.c.start + 12)
        svc.operator_step(wait=5, poll=0)
        server = serve(svc, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        lookup = manifest_lookup(f"http://127.0.0.1:{port}/api/v1/manifests/{{hash}}")
        watcher = Watcher(SimChain(w.sim), w.launcher_id, lambda cs: self.src, manifests=lookup, require_manifest=True)
        r = watcher.check()
        self.assertEqual(r.status, MATCH, r.to_json())
        self.assertEqual(r.holders, [])  # holder by holder, against the posted leaves
        self.assertNotIn("checked by replay alone", " ".join(r.notes))
        # A bare prefix works too ("<prefix>/<hash>.json").
        bare = Watcher(SimChain(w.sim), w.launcher_id, lambda cs: self.src, require_manifest=True,
                       manifests=manifest_lookup(f"http://127.0.0.1:{port}/api/v1/manifests"))
        self.assertEqual(bare.check().status, MATCH)
        # Someone edits the published file: the watcher catches it.
        path = self.manifests / f"{manifest_hash(svc.s.roots[1]).hex()}.json"
        data = json.loads(path.read_text())
        data["claims"][0]["cumulative"] += 1
        path.write_text(json.dumps(data))
        r = watcher.check()
        self.assertEqual(r.status, MISMATCH)
        fields = {d["field"] for d in r.diff}
        self.assertIn("manifest.file_hash", fields)
        self.assertTrue(r.holders)


class TestForeignSpends(ServiceTest):
    def posted_root_one(self):
        svc = self.service()
        self.w.advance_to(self.c.start + 12)
        self.assertEqual(svc.operator_step(wait=5, poll=0)["root_id"], 1)
        self.world_follows()
        return svc

    def test_a_dispute_is_noticed_and_nothing_is_posted(self):
        w = self.w
        svc = self.posted_root_one()
        w.dispute(1)  # someone else's bundle
        w.advance_to(w.state().pending.activates_at + 12)  # stale, the service would think a post is due
        before = self.op_balance()
        self.assertIsNone(svc.operator_step(wait=5, poll=0))
        self.assertEqual(self.op_balance(), before)  # nothing pushed
        st = svc.status()
        self.assertEqual(st["dispute"]["root_id"], 1)
        self.assertEqual(st["coin"], w.d.coin.coin_id().hex())
        self.assertEqual(svc.s.log[-1]["step"], "resync")
        self.assertIsNone(svc.s.mismatch)

    def test_a_veto_is_noticed_and_the_root_reposted_under_its_id(self):
        w = self.w
        svc = self.posted_root_one()
        w.veto(1)
        w.advance_to(w.h + 2)
        posted = svc.operator_step(wait=5, poll=0)
        self.assertEqual(posted["root_id"], 1)
        self.assertEqual(svc.status()["pending"]["root_id"], 1)

    def test_a_rotation_shows_on_the_next_check(self):
        w = self.w
        svc = self.posted_root_one()
        w.rotate("engine2")
        self.assertIsNone(svc.status()["rotation"])  # not yet checked
        self.assertTrue(svc.follow())  # the main loop's check
        self.assertEqual(svc.status()["rotation"]["engine_ph"], w.keys.ph("engine2").hex())
        self.assertFalse(svc.follow())  # nothing new

    def test_a_held_lease_defers_the_check(self):
        w = self.w
        svc = self.posted_root_one()
        w.rotate("engine2")
        svc.lease.take("q", svc.d.coin.coin_id())
        self.assertFalse(svc.follow())
        svc.lease.release("q")
        self.assertTrue(svc.follow())

    def test_a_create_by_another_builder_is_followed(self):
        w = self.w
        svc = self.service()
        self.world_follows()
        start = w.h + HEIGHT_WINDOW + 2
        c2 = w.campaign("creator", 500_000, start, start + 40, Kind.LP_HOLD, POOL2, claim_window=WINDOW, nonce=2)
        w.create(c2, "creator")
        self.assertTrue(svc.follow())
        self.assertIn(c2.id, svc.s.campaigns)
        self.assertIn(c2.id.hex(), [c["id"] for c in svc.status()["campaigns"]])


class TestBootReplaysEveryLiveCampaign(ServiceTest):
    def setUp(self):
        super().setUp()
        w = self.w
        self.c2 = w.campaign("creator", 500_000, self.c.start, self.c.end, Kind.LP_HOLD, POOL2, claim_window=WINDOW, nonce=2)
        w.create(self.c2, "creator")
        w.advance_to(self.c.start + 21)
        self.end = self.c.start + 20

    def test_a_root_that_dropped_a_campaign_is_a_mismatch(self):
        w = self.w
        _, dropped = run_epoch(EngineState(w.launcher_id, height=self.c.start), [self.c], self.src, self.end)
        w.post(dropped)  # the operator's key, leaving c2's holders out
        svc = self.service()
        self.assertIn("root 1", svc.s.mismatch)
        self.assertEqual(svc.s.roots, {})
        w.advance_to(w.state().pending.activates_at + 1)
        mine = svc.claimable(self.alice)
        self.assertEqual(mine["claims"], [])  # no proof past the mismatching root
        self.assertIn("root 1", mine["note"])
        self.assertIsNone(svc.due(w.h))  # and nothing is posted on top of it
        self.assertIsNone(svc.operator_step(wait=5, poll=0))

    def test_a_root_with_every_campaign_matches(self):
        w = self.w
        _, honest = run_epoch(EngineState(w.launcher_id, height=self.c.start), [self.c, self.c2], self.src, self.end)
        w.post(honest)
        svc = self.service()
        self.assertIsNone(svc.s.mismatch)
        self.assertEqual(svc.s.roots[1].root, honest.root)
        self.assertEqual(set(svc.s.roots[1].totals), {self.c.id, self.c2.id})

    def test_a_reboot_reuses_the_unchanged_roots(self):
        w = self.w
        _, honest = run_epoch(EngineState(w.launcher_id, height=self.c.start), [self.c, self.c2], self.src, self.end)
        w.post(honest)
        calls = []
        svc = CampaignService(SimChain(w.sim), w.launcher_id, "testnet11", lambda cs: calls.append(1) or self.src,
                              operator=self.op, every=10)
        svc.boot()
        self.assertEqual(len(calls), 1)
        svc.boot()
        self.assertEqual(len(calls), 1)  # root 1 is unchanged on chain: not rerun
        self.assertIsNone(svc.s.mismatch)


class TestBootWithNoCampaign(ServiceTest):
    create_campaign = False

    def test_the_engine_start_waits_for_the_first_campaign(self):
        w = self.w
        svc = self.service()
        self.assertIsNone(svc.s.engine.height)  # not 0: accrual must not start at genesis
        self.assertIsNone(svc.due(w.h))
        self.world_follows()
        start = w.h + HEIGHT_WINDOW + 2
        c = w.campaign("creator", 1_000_000, start, start + 40, Kind.LP_HOLD, POOL, claim_window=WINDOW)
        w.create(c, "creator")
        self.assertTrue(svc.follow())
        self.assertEqual(svc.s.engine.height, c.start)
        w.advance_to(c.start + 12)
        posted = svc.operator_step(wait=5, poll=0)
        self.assertEqual((posted["root_id"], posted["start"]), (1, c.start))


if __name__ == "__main__":
    unittest.main()
