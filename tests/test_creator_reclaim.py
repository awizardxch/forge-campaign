"""The creator's reclaim (owner, 2026-11-27: "unclaimed rewards after a campaign ends should have
about 48hrs then the deployer can claim it back and would close everything"), on the simulator:
the service quotes, the creator's own coin sends the reclaim message and reserves the network fee,
and the service pushes it beside the singleton's spend. The campaign is removed and what its
holders left goes to the creator."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from test_lock_actions import LockActionTest
from test_service_follow import WINDOW

try:
    from chia_wallet_sdk import Signature

    from flpcr.service.chain import bundle_json
    HAVE_CHAIN = True
except ImportError:  # pragma: no cover
    HAVE_CHAIN = False

from flpcr.clvm import sha256tree


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk missing")
class TestCreatorReclaim(LockActionTest):
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
        return svc.d.chain_state.campaigns[0].id

    def creator_spend(self, sender_ph, q, reserve=None, coin_amount=10**12):
        """A coin at `sender_ph` sending the quote's message to the quoted coin, with its change and fee."""
        fee = q["network_fee"] if reserve is None else reserve
        coin = self.w.sim.new_coin(sender_ph, coin_amount)
        spend = self.w.keys.spend(coin, self.w.keys.label_of(sender_ph), [
            [66, 23, bytes.fromhex(q["message"]), bytes.fromhex(q["coin_id"])],
            [51, sender_ph, coin_amount - fee],
            [52, fee],
        ])
        return bundle_json([spend], Signature.infinity())

    def test_the_creator_reclaims_and_the_campaign_closes(self):
        svc = self.svc
        self.w.advance_to(self.c.start + 2)
        cid = svc.d.chain_state.campaigns[0].id
        with self.assertRaisesRegex(ValueError, "claim window is open"):
            svc.reclaims.quote({"campaign": cid.hex()})
        self.finished()
        camp = svc.d.chain_state.campaign(cid)
        creator, left = camp.creator, camp.budget - camp.claimed
        q = svc.reclaims.quote({"campaign": cid.hex()})
        self.assertEqual(bytes.fromhex(q["message"]), b"k" + sha256tree(["flpcr-reclaim-v1", svc.d.launcher_id, cid]))
        self.assertEqual((q["creator"], q["coin_id"], q["amount"]), (creator.hex(), svc.d.coin.coin_id().hex(), left))
        self.assertTrue(svc.lease.busy(), "the service's own writers wait while the creator signs")
        # The message from any other address, or too small a fee, is refused before anything is pushed.
        stranger = self.w.keys.ph("stranger")
        with self.assertRaisesRegex(ValueError, "from the creator's address"):
            svc.reclaims.submit({"quote": q["quote"], "spend_bundle": self.creator_spend(stranger, q)}, wait=5, poll=0)
        with self.assertRaisesRegex(ValueError, "reserves"):
            svc.reclaims.submit({"quote": q["quote"], "spend_bundle": self.creator_spend(creator, q, reserve=q["network_fee"] - 1)},
                                wait=5, poll=0)
        before = self.w.balance(creator)
        out = svc.reclaims.submit({"quote": q["quote"], "spend_bundle": self.creator_spend(creator, q)}, wait=5, poll=0)
        self.assertEqual(out["status"], "confirmed")
        self.world_follows()
        self.assertIsNone(svc.d.chain_state.campaign(cid), "the campaign is removed: its slot is free")
        self.assertEqual(self.w.balance(creator) - before, left + 10**12 - q["network_fee"],
                         "what holders left, and the change of the creator's own coin")
        self.assertFalse(svc.lease.busy(), "the lease is released once the reclaim confirms")
        with self.assertRaisesRegex(ValueError, "already confirmed"):
            svc.reclaims.submit({"quote": q["quote"], "spend_bundle": self.creator_spend(creator, q)}, wait=5, poll=0)
        with self.assertRaisesRegex(ValueError, "no such campaign"):
            svc.reclaims.quote({"campaign": cid.hex()})

    def test_a_reclaim_waits_for_a_root_that_owes_holders_more(self):
        svc, w = self.svc, self.w
        cid = svc.d.chain_state.campaigns[0].id
        w.advance_to(self.c.end + WINDOW + 1)
        self.assertIsNotNone(svc.operator_step(wait=5, poll=0))
        with self.assertRaisesRegex(ValueError, "pending root owes this campaign's holders more"):
            svc.reclaims.quote({"campaign": cid.hex()})

    def test_a_quote_on_a_coin_that_moved_is_requoted(self):
        svc = self.svc
        cid = self.finished()
        q = svc.reclaims.quote({"campaign": cid.hex()})
        bundle = self.creator_spend(svc.d.chain_state.campaign(cid).creator, q)
        # Another builder spends the singleton (the admin's reclaim of the same campaign, from a lock).
        svc.lease.release(q["quote"])
        half = svc.lock_action("reclaim", self.admin, campaign_id=cid)
        self.land(half, self.admin)
        svc.lease.take(q["quote"], bytes.fromhex(q["coin_id"]))  # the creator's quote is still open
        with self.assertRaisesRegex(ValueError, "moved since the quote"):
            svc.reclaims.submit({"quote": q["quote"], "spend_bundle": bundle}, wait=5, poll=0)

    def test_the_routes(self):
        from flpcr.service.http import serve
        server = serve(self.svc, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        def post(path, body):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        status, body = post("/api/v1/reclaim/quote", {"campaign": "ab"})
        self.assertEqual(status, 400, body)
        cid = self.svc.d.chain_state.campaigns[0].id
        status, body = post("/api/v1/reclaim/quote", {"campaign": cid.hex()})
        self.assertEqual(status, 400, body)
        self.assertIn("claim window is open", body["error"])
        status, body = post("/api/v1/reclaim", {"quote": "nope", "spend_bundle": {"coin_spends": [], "aggregated_signature": ""}})
        self.assertEqual(status, 409, body)
        self.assertTrue(body["requote"])
        self.finished()
        status, body = post("/api/v1/reclaim/quote", {"campaign": cid.hex()})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mode"], 23)


if __name__ == "__main__":
    unittest.main()
