"""The 🔐 lock actions beside the veto (owner, 2026-11-26: "build the resolve, rotate and reclaim
lock proposals"), on the simulator. For each, the service builds the distributor's keyless half and
the one mode-23 condition the lock's coin carries; the half lands beside that message from the
right lock alone:

* resolve: the guardian or the admin settles an open dispute, upheld (the bond back to the
  disputer, the root revoked) or not (the bond to the dev fee address, the window restarted);
* rotate: the guardian proposes a new operator key, which waits out the rotation delay;
* cancel-rotation: the admin cancels a waiting rotation;
* reclaim: the admin returns what a campaign's holders left to its creator, past its claim window.
"""

import unittest

from test_service_follow import WINDOW, ServiceTest
from test_guardian_gate import coin_spends

try:
    from chia_wallet_sdk import Signature
    HAVE_CHAIN = True
except ImportError:  # pragma: no cover
    HAVE_CHAIN = False

from flpcr.clvm import sha256tree


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk missing")
class LockActionTest(ServiceTest):
    def setUp(self):
        super().setUp()
        self.svc = self.service()
        self.guardian = self.svc.d.cfg.guardian_ph
        self.admin = self.svc.d.cfg.admin_ph

    def lock_spend(self, sender_ph, half):
        """A coin at `sender_ph` carrying the one condition the half names (a 🔐 lock's spend does
        the same from its vault)."""
        w = self.w
        cond = half["condition"]
        coin = w.sim.new_coin(sender_ph, 1)
        return w.keys.spend(coin, w.keys.label_of(sender_ph),
                            [[cond[0], cond[1], bytes.fromhex(cond[2][2:]), bytes.fromhex(cond[3][2:])]])

    def land(self, half, sender_ph):
        """The half alone is refused; a coin at another address than the leaf's sender is refused;
        beside the right lock's message it lands, and the service follows it."""
        spends = coin_spends(half["spends"])
        self.assertEqual(half["coin_id"], self.svc.d.coin.coin_id().hex())
        self.assertEqual(half["condition"][:2], [66, 23])
        with self.assertRaises(Exception):
            self.svc.chain.push(spends, Signature.infinity())
        stranger = self.w.keys.ph("stranger")
        with self.assertRaises(Exception):
            self.svc.chain.push(spends + [self.lock_spend(stranger, half)], Signature.infinity())
        self.svc.chain.push(spends + [self.lock_spend(sender_ph, half)], Signature.infinity())
        self.assertTrue(self.svc.follow())
        self.assertIsNone(self.svc.lease.guardian(), "the hold ends once the coin it named is spent")
        self.world_follows()

    def posted(self):
        self.w.advance_to(self.c.start + 12)
        out = self.svc.operator_step(wait=5, poll=0)
        self.world_follows()
        return out["root_id"]


class TestResolve(LockActionTest):
    def disputed(self):
        rid = self.posted()
        self.w.dispute(rid)
        self.svc.follow()
        self.assertIsNotNone(self.svc.d.chain_state.dispute)
        return rid

    def test_the_guardian_upholds_and_the_bond_goes_back(self):
        rid = self.disputed()
        disputer = self.w.keys.ph("disputer")
        before = self.w.xch_balance(disputer)
        half = self.svc.lock_action("resolve", self.guardian, hold=True, root_id=rid, upheld=1)
        self.assertEqual(bytes.fromhex(half["message"]), b"s" + sha256tree([rid, 1]))
        self.assertEqual(half["bond_to"], disputer.hex())
        self.assertTrue(self.svc.lease.busy(), "held while the owners sign")
        self.land(half, self.guardian)
        st = self.svc.d.chain_state
        self.assertIsNone(st.dispute)
        self.assertIsNone(st.pending, "an upheld dispute revokes the root")
        self.assertEqual(self.w.xch_balance(disputer) - before, self.svc.d.cfg.dispute_bond)

    def test_the_admin_rejects_and_the_bond_is_forfeited(self):
        rid = self.disputed()
        dev = self.svc.d.cfg.dev_fee_ph
        before = self.w.xch_balance(dev)
        half = self.svc.lock_action("resolve", self.admin, root_id=rid, upheld=0)
        self.assertEqual(bytes.fromhex(half["message"]), b"s" + sha256tree([rid, 0]))
        self.land(half, self.admin)
        st = self.svc.d.chain_state
        self.assertIsNone(st.dispute)
        self.assertEqual(st.pending.root_id, rid, "the root stands, its window restarted")
        self.assertEqual(self.w.xch_balance(dev) - before, self.svc.d.cfg.dispute_bond)

    def test_refusals(self):
        rid = self.posted()
        with self.assertRaisesRegex(ValueError, "no dispute is open"):
            self.svc.lock_action("resolve", self.guardian, root_id=rid, upheld=1)
        self.w.dispute(rid)
        self.svc.follow()
        with self.assertRaisesRegex(ValueError, "neither the distributor's guardian nor its admin"):
            self.svc.lock_action("resolve", self.w.keys.ph("stranger"), root_id=rid, upheld=1)
        with self.assertRaisesRegex(ValueError, "not root"):
            self.svc.lock_action("resolve", self.guardian, root_id=rid + 1, upheld=1)
        with self.assertRaisesRegex(ValueError, "upheld is 1"):
            self.svc.lock_action("resolve", self.guardian, root_id=rid, upheld=2)
        with self.assertRaisesRegex(ValueError, "disputed: the guardian settles it with resolve"):
            self.svc.lock_action("veto", self.guardian, root_id=rid)
        self.assertIsNone(self.svc.lease.guardian(), "a refusal holds nothing")


class TestRotate(LockActionTest):
    def test_the_guardian_rotates_and_the_admin_cancels(self):
        new = self.w.keys.ph("new-operator")
        half = self.svc.lock_action("rotate", self.guardian, hold=True, engine_ph=new)
        self.assertEqual(bytes.fromhex(half["message"]), b"r" + new)
        self.assertEqual(half["current_engine_ph"], self.op.puzzle_hash.hex())
        self.land(half, self.guardian)
        rotation = self.svc.d.chain_state.rotation
        self.assertEqual(rotation.engine_ph, new, "waiting out the delay")
        self.assertGreater(rotation.effective_at, self.w.h)

        with self.assertRaisesRegex(ValueError, "not the distributor's admin"):
            self.svc.lock_action("cancel-rotation", self.guardian, engine_ph=new)
        with self.assertRaisesRegex(ValueError, "the waiting rotation is to"):
            self.svc.lock_action("cancel-rotation", self.admin, engine_ph=self.w.keys.ph("other"))
        half = self.svc.lock_action("cancel-rotation", self.admin, hold=True, engine_ph=new)
        self.assertEqual(bytes.fromhex(half["message"]), b"x" + new)
        self.land(half, self.admin)
        self.assertIsNone(self.svc.d.chain_state.rotation)

    def test_refusals(self):
        with self.assertRaisesRegex(ValueError, "not the distributor's guardian"):
            self.svc.lock_action("rotate", self.admin, engine_ph=self.w.keys.ph("new-operator"))
        with self.assertRaisesRegex(ValueError, "already the distributor's operator"):
            self.svc.lock_action("rotate", self.guardian, engine_ph=self.op.puzzle_hash)
        with self.assertRaisesRegex(ValueError, "no rotation is waiting"):
            self.svc.lock_action("cancel-rotation", self.admin, engine_ph=self.w.keys.ph("new-operator"))
        with self.assertRaisesRegex(ValueError, "32-byte"):
            self.svc.lock_action("rotate", self.guardian, engine_ph=b"\x01")
        with self.assertRaisesRegex(ValueError, "the lock actions are"):
            self.svc.lock_action("delegate", self.guardian)


class TestAdminReclaim(LockActionTest):
    def finished(self):
        """Every root of the campaign posted and active, its claim window passed."""
        w, svc = self.w, self.svc
        for _ in range(12):
            w.advance_to(w.h + 12)
            svc.operator_step(wait=5, poll=0)
            if w.h > self.c.end + WINDOW and svc.d.chain_state.pending is None:
                break
        w.advance_to(max(w.h, self.c.end + WINDOW) + 1)
        svc.follow()
        self.world_follows()
        return self.c.campaign_id if hasattr(self.c, "campaign_id") else svc.d.chain_state.campaigns[0].id

    def test_the_admin_reclaims_for_the_creator(self):
        svc = self.svc
        self.w.advance_to(self.c.start + 2)
        cid = svc.d.chain_state.campaigns[0].id
        with self.assertRaisesRegex(ValueError, "claim window is open"):
            svc.lock_action("reclaim", self.admin, campaign_id=cid)
        self.finished()
        camp = svc.d.chain_state.campaign(cid)
        creator = camp.creator
        before = self.w.balance(creator)
        with self.assertRaisesRegex(ValueError, "not the distributor's admin"):
            svc.lock_action("reclaim", self.guardian, campaign_id=cid)
        half = svc.lock_action("reclaim", self.admin, hold=True, campaign_id=cid)
        self.assertEqual(bytes.fromhex(half["message"]),
                         b"k" + sha256tree(["flpcr-reclaim-v1", svc.d.launcher_id, cid]))
        self.assertEqual(half["creator"], creator.hex())
        left = half["amount"]
        self.land(half, self.admin)
        self.assertIsNone(svc.d.chain_state.campaign(cid), "the campaign is removed")
        self.assertEqual(self.w.balance(creator) - before, left, "what holders left goes to the creator")
        with self.assertRaisesRegex(ValueError, "no such campaign"):
            svc.lock_action("reclaim", self.admin, campaign_id=cid)

    def test_a_reclaim_waits_for_a_root_that_owes_holders_more(self):
        svc, w = self.svc, self.w
        cid = svc.d.chain_state.campaigns[0].id
        w.advance_to(self.c.end + WINDOW + 1)  # past the claim window with nothing posted for it yet
        out = svc.operator_step(wait=5, poll=0)
        self.assertIsNotNone(out, "the operator posts the campaign's root late")
        st = svc.d.chain_state
        self.assertGreater(st.pending.total_for(cid), st.active.total_for(cid) if st.active else 0)
        with self.assertRaisesRegex(ValueError, "pending root owes this campaign's holders more"):
            svc.lock_action("reclaim", self.admin, campaign_id=cid)
        w.advance_to(st.pending.activates_at)
        svc.lock_action("reclaim", self.admin, campaign_id=cid)  # active now, as the leaf's prologue sees it


class TestRoutes(LockActionTest):
    """POST /api/v1/guardian/<action>: what the Forge router calls over the compose network."""

    def post(self, path, body):
        import json
        import threading
        import urllib.error
        import urllib.request

        from flpcr.service.http import serve
        if not hasattr(self, "port"):
            server = serve(self.svc, 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown)
            self.port = server.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_each_action_is_routed_and_its_fields_checked(self):
        new = self.w.keys.ph("new-operator")
        status, body = self.post("/api/v1/guardian/rotate", {"sender": self.guardian.hex(), "engine_ph": new.hex()})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["action"], "rotate")
        self.assertEqual(bytes.fromhex(body["message"]), b"r" + new)
        self.assertFalse(body["held"])
        status, body = self.post("/api/v1/guardian/resolve", {"sender": self.guardian.hex(), "root_id": 1, "upheld": 2})
        self.assertEqual(status, 400)
        self.assertIn("upheld", body["error"])
        status, body = self.post("/api/v1/guardian/reclaim", {"sender": self.admin.hex(), "campaign_id": "ab"})
        self.assertEqual(status, 400)
        self.assertIn("campaign_id", body["error"])
        status, body = self.post("/api/v1/guardian/delegate", {"sender": self.guardian.hex()})
        self.assertEqual(status, 404)
        # The veto keeps its old field name.
        rid = self.posted()
        status, body = self.post("/api/v1/guardian/veto", {"guardian": self.guardian.hex(), "root_id": rid, "hold": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["held"])


if __name__ == "__main__":
    unittest.main()
