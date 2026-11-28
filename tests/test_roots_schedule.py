"""When the operator posts a root (owner, 2026-11-28). Mainnet follows Merkl: a root on a schedule
(FLPCR_EVERY, 8 hours there) and each campaign's final root. On-demand roots are an option
(FLPCR_ROOTS=on-demand): a root only when a holder asks (Claim) or a campaign ends, at most one
per `every` blocks however often anyone asks."""

import unittest

from test_service_follow import ServiceTest


class TestOnDemandRoots(ServiceTest):
    def test_no_root_until_someone_asks(self):
        svc, w = self.service(every=10, on_demand=True), self.w
        self.assertEqual(svc.request_root()["requested"], False, "nothing has accrued before the start")
        w.advance_to(self.c.start + 25)
        self.assertIsNone(svc.operator_step(wait=5, poll=0), "no request, no campaign ended: no root")
        out = svc.request_root()
        self.assertTrue(out["requested"])
        self.assertEqual(out["claimable_from"], out["posts_at"] + svc.d.cfg.dispute_blocks)
        self.assertTrue(svc.status()["root_requested"])
        posted = svc.operator_step(wait=5, poll=0)
        self.assertIsNotNone(posted, "a request posts the stretch since the last root")
        self.assertEqual(posted["start"], self.c.start)
        self.assertFalse(svc.root_requested, "the root answers the request")
        w.advance_to(w.h + 3)
        self.assertIsNone(svc.operator_step(wait=5, poll=0), "and no more roots without another request")

    def test_requests_post_no_more_often_than_every(self):
        """Asked for before the stretch is `every` blocks long, the root waits; it goes up once it is."""
        svc, w = self.service(every=25, on_demand=True), self.w  # the campaign runs 40 blocks
        w.advance_to(self.c.start + 12)
        svc.request_root()
        self.assertIsNone(svc.operator_step(wait=5, poll=0), "12 blocks since the start: too soon")
        self.assertTrue(svc.root_requested, "the request is kept")
        w.advance_to(self.c.start + 26)
        posted = svc.operator_step(wait=5, poll=0)
        self.assertIsNotNone(posted, "26 blocks: the requested root goes up")
        self.assertLess(posted["end"], self.c.end, "a requested root, not the final one")

    def test_a_campaign_that_ends_gets_its_final_root_unasked(self):
        svc, w = self.service(every=1000, on_demand=True), self.w
        w.advance_to(self.c.end + 1)
        posted = svc.operator_step(wait=5, poll=0)
        self.assertIsNotNone(posted, "the final root goes up without a request")
        self.assertEqual(posted["end"], self.c.end)


class TestRootMargin(ServiceTest):
    """A root ends `root_margin` blocks behind the tip (owner, 2026-11-28): the freshest blocks wait
    for the next root, and a campaign's final root goes up that many blocks after it ends."""

    def test_a_root_ends_behind_the_tip(self):
        svc, w = self.service(every=10, root_margin=5), self.w
        w.advance_to(self.c.start + 12)
        self.assertIsNone(svc.operator_step(wait=5, poll=0), "12 blocks in, only 7 are settled: under `every`")
        w.advance_to(self.c.start + 16)
        posted = svc.operator_step(wait=5, poll=0)
        self.assertIsNotNone(posted)
        self.assertEqual(posted["end"], self.c.start + 16 - 5, "the root stops 5 blocks short of the tip")

    def test_the_final_root_waits_for_the_margin(self):
        svc, w = self.service(every=1000, root_margin=5), self.w
        w.advance_to(self.c.end + 2)
        self.assertIsNone(svc.operator_step(wait=5, poll=0), "2 blocks after the end: its last blocks are not settled")
        w.advance_to(self.c.end + 5)
        posted = svc.operator_step(wait=5, poll=0)
        self.assertIsNotNone(posted, "5 blocks after the end: the final root")
        self.assertEqual(posted["end"], self.c.end)


class TestScheduledRoots(ServiceTest):
    def test_scheduled_is_the_default_and_ignores_requests(self):
        svc, w = self.service(every=10), self.w
        self.assertFalse(svc.on_demand)
        self.assertEqual(svc.status()["roots"], "scheduled")
        self.assertEqual(svc.request_root()["requested"], False)
        w.advance_to(self.c.start + 12)
        self.assertIsNotNone(svc.operator_step(wait=5, poll=0), "a root every `every` blocks, unasked")


if __name__ == "__main__":
    unittest.main()
