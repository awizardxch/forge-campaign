"""Address links: a holder's Wizard at one address boosts or unlocks LP at another."""

import unittest

try:
    from chia_wallet_sdk import SecretKey, standard_puzzle_hash

    HAVE_SDK = True
except ImportError:  # pragma: no cover
    HAVE_SDK = False

from flpcr import Campaign, FixtureSource, HolderEvent, Kind, Params, accrue
from flpcr.links import Link, LinkBook, link_message, sign, signed_digest, verify
from flpcr.clvm import sha256tree
from flpcr.model import Boost, Holding, Requirement

POOL, LP, WIZARDS = b"\xb0" * 32, b"\x1b" * 32, b"\x33" * 32
GATE = Params(gates=(Requirement(Holding.NFT, WIZARDS),))


class Address:
    def __init__(self, seed: int):
        self.sk = SecretKey.from_seed(bytes([seed]) * 32).derive_synthetic()
        self.ph = standard_puzzle_hash(self.sk.public_key())


def link(primary: Address, member: Address, height: int) -> Link:
    mpk, msig = sign(member.sk, primary.ph, member.ph)
    ppk, psig = sign(primary.sk, primary.ph, member.ph)
    return Link(primary.ph, member.ph, height, mpk, msig, ppk, psig)


def unlink(member: Address, height: int) -> Link:
    pk, sig = sign(member.sk, member.ph, member.ph)
    return Link(member.ph, member.ph, height, pk, sig)


def campaign(params=GATE):
    return Campaign(b"\xd1" * 32, b"\xcc" * 32, b"\xca" * 32, 1000, 100, 200, Kind.LP_HOLD, POOL, params, fee_bps=0)


@unittest.skipUnless(HAVE_SDK, "chia-wallet-sdk missing")
class TestLinks(unittest.TestCase):
    def setUp(self):
        self.vault, self.lp_addr, self.other, self.stranger = (Address(i) for i in (1, 2, 3, 4))

    def source(self, links, nfts=None, lp=None):
        lp = lp or [HolderEvent(0, self.lp_addr.ph, 10), HolderEvent(0, self.other.ph, 10)]
        nfts = nfts if nfts is not None else [HolderEvent(0, self.vault.ph, 1)]
        return FixtureSource(lp_assets={POOL: LP}, events={LP: lp}, nfts={WIZARDS: nfts}, address_links=links)

    def test_message_format_is_chip0002(self):
        m = link_message(b"\x01" * 32, b"\x02" * 32)
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
        one_sided = Link(good.primary, good.member, 0, good.member_pk, good.member_sig,
                         good.member_pk, good.member_sig)
        self.assertFalse(verify(one_sided))
        # Someone else's key signing for the Wizard address.
        spk, ssig = sign(self.stranger.sk, self.vault.ph, self.lp_addr.ph)
        forged = Link(good.primary, good.member, 0, good.member_pk, good.member_sig, spk, ssig)
        self.assertFalse(verify(forged))
        for bad in (one_sided, forged):
            self.assertEqual(accrue(campaign(), self.source([bad]), 100, 200), {})

    def test_link_counts_from_its_height_until_unlinked(self):
        links = [link(self.vault, self.lp_addr, 150), unlink(self.lp_addr, 170)]
        credit = accrue(campaign(), self.source(links), 100, 200)
        self.assertEqual(credit, {self.lp_addr.ph: 1000 * 20 // 100})

    def test_holdings_sum_for_boost_tiers(self):
        # 2 Wizards at one address, 1 at another, LP at a third: linked, the group holds 3.
        tier = Params(boosts=(Boost(Requirement(Holding.NFT, WIZARDS, 3), 10000),))
        nfts = [HolderEvent(0, self.vault.ph, 2), HolderEvent(0, self.stranger.ph, 1)]
        links = [link(self.vault, self.lp_addr, 0), link(self.vault, self.stranger, 0)]
        credit = accrue(campaign(tier), self.source(links, nfts=nfts), 100, 200)
        self.assertEqual(credit[self.lp_addr.ph], 2 * credit[self.other.ph])

    def test_groups_are_one_level_deep(self):
        a, b, c = self.vault, self.lp_addr, self.other
        book = LinkBook([link(a, b, 0), link(b, c, 1), link(c, a, 2)])
        self.assertEqual(book.group(b.ph, 5), frozenset({a.ph, b.ph}))
        self.assertEqual(book.group(c.ph, 5), frozenset({c.ph}))

    def test_relinking_moves_an_address(self):
        a, b, c = self.vault, self.lp_addr, self.other
        book = LinkBook([link(a, b, 0), link(c, b, 10)])
        self.assertEqual(book.group(b.ph, 5), frozenset({a.ph, b.ph}))
        self.assertEqual(book.group(b.ph, 10), frozenset({c.ph, b.ph}))
        self.assertEqual(book.group(a.ph, 10), frozenset({a.ph}))

    def test_group_size_cap(self):
        primary = self.vault
        members = [Address(10 + i) for i in range(4)]
        book = LinkBook([link(primary, m, 0) for m in members], max_members=3)
        self.assertEqual(len(book.group(primary.ph, 0)), 4)  # primary + 3



@unittest.skipUnless(HAVE_SDK, "chia-wallet-sdk missing")
class TestPublication(unittest.TestCase):
    def test_round_trip_and_reverify(self):
        from flpcr.links import link_from_json, link_to_json

        original = link(Address(1), Address(2), 7)
        again = link_from_json(link_to_json(original))
        self.assertEqual(again, original)
        self.assertTrue(verify(again))
        self.assertTrue(verify(link_from_json(link_to_json(unlink(Address(2), 9)))))


if __name__ == "__main__":
    unittest.main()
