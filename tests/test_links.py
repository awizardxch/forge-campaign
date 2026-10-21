"""Address links: a holder's Wizard at one address boosts or unlocks LP at another."""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

try:
    from chia_wallet_sdk import PublicKey, SecretKey, Signature, standard_puzzle_hash

    HAVE_SDK = True
except ImportError:  # pragma: no cover
    HAVE_SDK = False

from flpcr import Campaign, EngineState, FixtureSource, HolderEvent, Kind, Params, accrue, run_epoch
from flpcr.clvm import sha256tree
from flpcr.links import (
    EMPTY_LOG_HASH,
    Link,
    LinkBook,
    link_from_json,
    link_message,
    link_to_json,
    log_hash,
    log_to_json,
    sign,
    signed_digest,
    verify,
)
from flpcr.manifest import root_to_json, state_from_json, state_to_json
from flpcr.model import Boost, Holding, Requirement

DIST = b"\xd1" * 32
OTHER_DIST = b"\xd2" * 32
POOL, LP, WIZARDS = b"\xb0" * 32, b"\x1b" * 32, b"\x33" * 32
GATE = Params(gates=(Requirement(Holding.NFT, WIZARDS),))


class Address:
    def __init__(self, seed: int):
        self.sk = SecretKey.from_seed(bytes([seed]) * 32).derive_synthetic()
        self.ph = standard_puzzle_hash(self.sk.public_key())


def link(primary: Address, member: Address, height: int, seq: int = 1, dist: bytes = DIST) -> Link:
    mpk, msig = sign(member.sk, dist, primary.ph, member.ph, seq)
    ppk, psig = sign(primary.sk, dist, primary.ph, member.ph, seq)
    return Link(dist, primary.ph, member.ph, seq, height, False, mpk, msig, ppk, psig)


def unlink(primary: Address, member: Address, height: int, seq: int, by: Address) -> Link:
    """An unlink of (primary, member), signed by `by` alone (either side)."""
    pk, sig = sign(by.sk, DIST, primary.ph, member.ph, seq, unlink=True)
    if by is member:
        return Link(DIST, primary.ph, member.ph, seq, height, True, member_pk=pk, member_sig=sig)
    return Link(DIST, primary.ph, member.ph, seq, height, True, primary_pk=pk, primary_sig=sig)


def at_height(entry: Link, height: int) -> Link:
    """The same signed entry, recorded again at another height (a replay)."""
    return Link(entry.distributor_id, entry.primary, entry.member, entry.seq, height, entry.unlink,
                entry.member_pk, entry.member_sig, entry.primary_pk, entry.primary_sig)


def campaign(params=GATE):
    return Campaign(DIST, b"\xcc" * 32, b"\xca" * 32, 1000, 100, 200, Kind.LP_HOLD, POOL, params, fee_bps=0)


@unittest.skipUnless(HAVE_SDK, "chia-wallet-sdk missing")
class TestLinks(unittest.TestCase):
    def setUp(self):
        self.vault, self.lp_addr, self.other, self.stranger = (Address(i) for i in (1, 2, 3, 4))

    def source(self, links, nfts=None, lp=None):
        lp = lp or [HolderEvent(0, self.lp_addr.ph, 10), HolderEvent(0, self.other.ph, 10)]
        nfts = nfts if nfts is not None else [HolderEvent(0, self.vault.ph, 1)]
        return FixtureSource(lp_assets={POOL: LP}, events={LP: lp}, nfts={WIZARDS: nfts}, address_links=links)

    def test_message_format_is_chip0002_and_v2(self):
        m = link_message(DIST, b"\x01" * 32, b"\x02" * 32, 7)
        self.assertEqual(m, f"flpcr-link-v2:{DIST.hex()}:{'01' * 32}:{'02' * 32}:7")
        self.assertTrue(link_message(DIST, b"\x01" * 32, b"\x02" * 32, 7, unlink=True).startswith("flpcr-unlink-v2:"))
        self.assertEqual(signed_digest(m), sha256tree(("Chia Signed Message", m)))

    def test_without_a_link_the_wizard_elsewhere_does_not_count(self):
        self.assertEqual(accrue(campaign(), self.source([]), 100, 200), {})

    def test_linked_addresses_share_the_wizard(self):
        credit = accrue(campaign(), self.source([link(self.vault, self.lp_addr, 0)]), 100, 200)
        self.assertEqual(credit, {self.lp_addr.ph: 1000})  # paid to the LP address

    def test_both_sides_must_sign(self):
        good = link(self.vault, self.lp_addr, 0)
        self.assertTrue(verify(good))
        # The LP address alone claims the Wizard holder as its primary.
        one_sided = Link(DIST, good.primary, good.member, 1, 0, False, good.member_pk, good.member_sig,
                         good.member_pk, good.member_sig)
        self.assertFalse(verify(one_sided))
        # Someone else's key signing for the Wizard address.
        spk, ssig = sign(self.stranger.sk, DIST, self.vault.ph, self.lp_addr.ph, 1)
        forged = Link(DIST, good.primary, good.member, 1, 0, False, good.member_pk, good.member_sig, spk, ssig)
        self.assertFalse(verify(forged))
        for bad in (one_sided, forged):
            self.assertEqual(accrue(campaign(), self.source([bad]), 100, 200), {})

    def test_signature_is_bound_to_distributor_and_seq(self):
        good = link(self.vault, self.lp_addr, 0)
        moved = Link(OTHER_DIST, good.primary, good.member, 1, 0, False, good.member_pk, good.member_sig,
                     good.primary_pk, good.primary_sig)
        self.assertFalse(verify(moved))
        bumped = Link(DIST, good.primary, good.member, 2, 0, False, good.member_pk, good.member_sig,
                      good.primary_pk, good.primary_sig)
        self.assertFalse(verify(bumped))
        # A link signed for another distributor is not this distributor's.
        foreign = link(self.vault, self.lp_addr, 0, dist=OTHER_DIST)
        self.assertTrue(verify(foreign))
        self.assertEqual(accrue(campaign(), self.source([foreign]), 100, 200), {})

    def test_link_counts_from_its_height_until_unlinked(self):
        links = [link(self.vault, self.lp_addr, 150), unlink(self.vault, self.lp_addr, 170, 2, by=self.lp_addr)]
        credit = accrue(campaign(), self.source(links), 100, 200)
        self.assertEqual(credit, {self.lp_addr.ph: 1000 * 20 // 100})

    def test_primary_can_unlink(self):
        """FLPCR-11: the Wizard holder can take back a link without the member."""
        links = [link(self.vault, self.lp_addr, 0), unlink(self.vault, self.lp_addr, 150, 2, by=self.vault)]
        credit = accrue(campaign(), self.source(links), 100, 200)
        self.assertEqual(credit, {self.lp_addr.ph: 500})

    def test_unlink_signed_by_a_third_party_is_ignored(self):
        links = [link(self.vault, self.lp_addr, 0), unlink(self.vault, self.lp_addr, 150, 2, by=self.stranger)]
        self.assertEqual(accrue(campaign(), self.source(links), 100, 200), {self.lp_addr.ph: 1000})

    def test_replayed_link_cannot_undo_an_unlink(self):
        """FLPCR-11: the old signed link, recorded again after the unlink, is refused."""
        old = link(self.vault, self.lp_addr, 10)
        gone = unlink(self.vault, self.lp_addr, 50, 2, by=self.lp_addr)
        control = accrue(campaign(), self.source([old, gone]), 100, 200)
        self.assertEqual(control, {})
        replayed = accrue(campaign(), self.source([old, gone, at_height(old, 150)]), 100, 200)
        self.assertEqual(replayed, control)
        # A fresh consent, with a higher seq, is how the pair links again.
        again = accrue(campaign(), self.source([old, gone, link(self.vault, self.lp_addr, 150, seq=3)]), 100, 200)
        self.assertEqual(again, {self.lp_addr.ph: 500})

    def test_rejected_entry_still_uses_its_seq(self):
        # The unlink at seq 1 does nothing (no group yet), but the link signed
        # with seq 1 can no longer be recorded after it.
        links = [unlink(self.vault, self.lp_addr, 0, 1, by=self.lp_addr), link(self.vault, self.lp_addr, 10, seq=1)]
        self.assertEqual(accrue(campaign(), self.source(links), 100, 200), {})

    def test_holdings_sum_for_boost_tiers(self):
        # 2 Wizards at one address, 1 at another, LP at a third: linked, the group holds 3.
        tier = Params(boosts=(Boost(Requirement(Holding.NFT, WIZARDS, 3), 10000),))
        nfts = [HolderEvent(0, self.vault.ph, 2), HolderEvent(0, self.stranger.ph, 1)]
        links = [link(self.vault, self.lp_addr, 0), link(self.vault, self.stranger, 0)]
        credit = accrue(campaign(tier), self.source(links, nfts=nfts), 100, 200)
        self.assertEqual(credit[self.lp_addr.ph], 2 * credit[self.other.ph])

    def test_groups_are_one_level_deep(self):
        a, b, c = self.vault, self.lp_addr, self.other
        book = LinkBook([link(a, b, 0), link(b, c, 1), link(c, a, 2)], DIST)
        self.assertEqual(book.group(b.ph, 5), frozenset({a.ph, b.ph}))
        self.assertEqual(book.group(c.ph, 5), frozenset({c.ph}))

    def test_relinking_moves_an_address(self):
        a, b, c = self.vault, self.lp_addr, self.other
        book = LinkBook([link(a, b, 0), link(c, b, 10)], DIST)
        self.assertEqual(book.group(b.ph, 5), frozenset({a.ph, b.ph}))
        self.assertEqual(book.group(b.ph, 10), frozenset({c.ph, b.ph}))
        self.assertEqual(book.group(a.ph, 10), frozenset({a.ph}))

    def test_group_size_cap(self):
        primary = self.vault
        members = [Address(10 + i) for i in range(4)]
        book = LinkBook([link(primary, m, 0) for m in members], DIST, max_members=3)
        self.assertEqual(len(book.group(primary.ph, 0)), 4)  # primary + 3

    def test_log_heights_must_not_go_backwards(self):
        with self.assertRaises(ValueError):
            LinkBook([link(self.vault, self.lp_addr, 10), link(self.other, self.stranger, 5)], DIST)

    def test_identity_key_never_signs(self):
        inf, s0 = PublicKey.infinity(), Signature.infinity()
        ph = standard_puzzle_hash(inf)
        entry = Link(DIST, self.vault.ph, ph, 1, 0, True, member_pk=inf.to_bytes(), member_sig=s0.to_bytes())
        self.assertFalse(verify(entry))


@unittest.skipUnless(HAVE_SDK, "chia-wallet-sdk missing")
class TestLinkLog(unittest.TestCase):
    """FLPCR-11: the log a root used is committed in its manifest, and a root
    cannot be built on a log that rewrites what an earlier root committed."""

    def setUp(self):
        self.vault, self.lp_addr, self.other = Address(1), Address(2), Address(3)
        self.lp = [HolderEvent(0, self.lp_addr.ph, 10), HolderEvent(0, self.other.ph, 10)]
        self.c = campaign()

    def source(self, links):
        return FixtureSource(lp_assets={POOL: LP}, events={LP: self.lp},
                             nfts={WIZARDS: [HolderEvent(0, self.vault.ph, 1)]}, address_links=links)

    def test_manifest_commits_the_log_prefix(self):
        log = [link(self.vault, self.lp_addr, 120), unlink(self.vault, self.lp_addr, 180, 2, by=self.vault)]
        state, root = run_epoch(EngineState(DIST, height=100), [self.c], self.source(log), 150)
        self.assertEqual(root.link_count, 1)  # the unlink at 180 is after this root
        self.assertEqual(root.links_hash, log_hash(log[:1]))
        self.assertEqual(root_to_json(root)["links"], {"count": 1, "sha256": log_hash(log[:1]).hex()})
        self.assertEqual(state_from_json(state_to_json(state)), state)
        _, root2 = run_epoch(state, [self.c], self.source(log), 200)
        self.assertEqual((root2.link_count, root2.links_hash), (2, log_hash(log)))

    def test_backdated_entry_is_refused(self):
        first = [link(self.vault, self.lp_addr, 120)]
        state, _ = run_epoch(EngineState(DIST, height=100), [self.c], self.source(first), 150)
        # A link recorded after root 1 but dated inside its window.
        late = first + [link(self.vault, self.other, 140)]
        with self.assertRaises(ValueError):
            run_epoch(state, [self.c], self.source(late), 200)
        # Dated at or after the last root's end, it is fine.
        ok = first + [link(self.vault, self.other, 150)]
        run_epoch(state, [self.c], self.source(ok), 200)

    def test_rewritten_prefix_is_refused(self):
        first = [link(self.vault, self.lp_addr, 120)]
        state, _ = run_epoch(EngineState(DIST, height=100), [self.c], self.source(first), 150)
        self.assertNotEqual(state.links_hash, EMPTY_LOG_HASH)
        with self.assertRaises(ValueError):
            run_epoch(state, [self.c], self.source([]), 200)  # the committed entry dropped
        with self.assertRaises(ValueError):
            run_epoch(state, [self.c], self.source([link(self.vault, self.other, 120)]), 200)


@unittest.skipUnless(HAVE_SDK, "chia-wallet-sdk missing")
class TestPublication(unittest.TestCase):
    def test_round_trip_and_reverify(self):
        a, b = Address(1), Address(2)
        original = link(a, b, 7)
        again = link_from_json(link_to_json(original))
        self.assertEqual(again, original)
        self.assertTrue(verify(again))
        self.assertTrue(verify(link_from_json(link_to_json(unlink(a, b, 9, 2, by=a)))))

    def test_fixture_json_keeps_links(self):
        """FLPCR-11: the fixture and CLI path reads the published log."""
        log = [link(Address(1), Address(2), 7)]
        self.assertEqual(FixtureSource.from_json({"links": log_to_json(log)}).links(), log)

    def test_cli_publishes_the_committed_log(self):
        from flpcr.__main__ import main

        vault, lp_addr = Address(1), Address(2)
        log = [link(vault, lp_addr, 120), link(vault, Address(3), 500)]
        fixture = {
            "campaigns": [{
                "distributor_id": DIST.hex(), "creator": "cc" * 32, "reward_asset": "ca" * 32, "amount": 1000,
                "start": 100, "end": 200, "kind": "LP_HOLD", "target": POOL.hex(), "fee_bps": 0,
                "params": {"gates": [{"holding": "NFT", "asset": WIZARDS.hex()}]},
            }],
            "lp_assets": {POOL.hex(): LP.hex()},
            "events": {LP.hex(): [[0, lp_addr.ph.hex(), 10]]},
            "nfts": {WIZARDS.hex(): [[0, vault.ph.hex(), 1]]},
            "links": log_to_json(log),
        }
        with tempfile.TemporaryDirectory() as d:
            path, out_log = os.path.join(d, "fixture.json"), os.path.join(d, "links.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(fixture, f)
            buf = StringIO()
            with redirect_stdout(buf):
                main([path, "--end", "200", "--link-log", out_log])
            manifest = json.loads(buf.getvalue())
            with open(out_log, encoding="utf-8") as f:
                published = json.load(f)
        self.assertEqual(manifest["totals"], {campaign().id.hex(): 800})  # linked from 120
        self.assertEqual(published, log_to_json(log[:1]))
        self.assertEqual(manifest["links"], {"count": 1, "sha256": log_hash(log[:1]).hex()})


if __name__ == "__main__":
    unittest.main()
