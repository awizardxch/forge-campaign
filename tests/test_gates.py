"""Token gates and boosts: hold a Wizard NFT to earn at all, hold more to earn more."""

import unittest
from fractions import Fraction

from flpcr import Campaign, FixtureSource, HolderEvent, Kind, Params, accrue
from flpcr.model import Boost, Holding, Requirement

DIST, CREATOR, REWARD = b"\xd1" * 32, b"\xcc" * 32, b"\xca" * 32
POOL, LP, VAULT, VAULT_LP = b"\xb0" * 32, b"\x1b" * 32, b"\x7a" * 32, b"\x2b" * 32
WIZARDS = b"\x33" * 32  # the DID that minted the Wizard collection
SPELL = b"\x5e" * 32  # some CAT
ALICE, BOB, CAROL = (bytes([x]) * 32 for x in (0xA1, 0xB2, 0xC3))

WIZARD = Requirement(Holding.NFT, WIZARDS)


def campaign(params: Params, amount: int = 1000) -> Campaign:
    return Campaign(DIST, CREATOR, REWARD, amount, 100, 200, Kind.LP_HOLD, POOL, params, fee_bps=0)


def source(lp, nfts=(), cats=(), wrappers=None):
    events = {LP: list(lp), SPELL: list(cats)}
    if wrappers:
        events[VAULT_LP] = list(wrappers)
    return FixtureSource(
        lp_assets={POOL: LP},
        events=events,
        nfts={WIZARDS: list(nfts)},
        wrappers={LP: {VAULT: VAULT_LP}} if wrappers else {},
    )


class TestGates(unittest.TestCase):
    def test_only_wizard_holders_earn(self):
        src = source([HolderEvent(0, ALICE, 10), HolderEvent(0, BOB, 10)], nfts=[HolderEvent(0, ALICE, 1)])
        credit = accrue(campaign(Params(gates=(WIZARD,))), src, 100, 200)
        self.assertEqual(credit, {ALICE: 1000})

    def test_gate_is_checked_block_by_block(self):
        # Bob holds a Wizard only for [150, 160): he shares those ten blocks.
        src = source(
            [HolderEvent(0, ALICE, 10), HolderEvent(0, BOB, 10)],
            nfts=[HolderEvent(0, ALICE, 1), HolderEvent(150, BOB, 1), HolderEvent(160, BOB, -1)],
        )
        credit = accrue(campaign(Params(gates=(WIZARD,))), src, 100, 200)
        self.assertEqual(credit[BOB], 50)  # half of 10 blocks x 10 per block
        self.assertEqual(credit[ALICE], 950)

    def test_flash_nft_earns_one_block(self):
        src = source(
            [HolderEvent(0, ALICE, 10), HolderEvent(0, BOB, 10)],
            nfts=[HolderEvent(0, ALICE, 1), HolderEvent(150, BOB, 1), HolderEvent(151, BOB, -1)],
        )
        credit = accrue(campaign(Params(gates=(WIZARD,))), src, 100, 200)
        self.assertEqual(credit[BOB], 5)

    def test_minimum_cat_gate(self):
        need = Requirement(Holding.CAT, SPELL, 1000)
        src = source(
            [HolderEvent(0, ALICE, 10), HolderEvent(0, BOB, 10)],
            cats=[HolderEvent(0, ALICE, 1000), HolderEvent(0, BOB, 999)],
        )
        self.assertEqual(accrue(campaign(Params(gates=(need,))), src, 100, 200), {ALICE: 1000})

    def test_every_gate_must_hold(self):
        need = Requirement(Holding.CAT, SPELL, 1000)
        src = source(
            [HolderEvent(0, ALICE, 10), HolderEvent(0, BOB, 10), HolderEvent(0, CAROL, 10)],
            nfts=[HolderEvent(0, ALICE, 1), HolderEvent(0, BOB, 1)],
            cats=[HolderEvent(0, ALICE, 1000), HolderEvent(0, CAROL, 1000)],
        )
        credit = accrue(campaign(Params(gates=(WIZARD, need))), src, 100, 200)
        self.assertEqual(credit, {ALICE: 1000})

    def test_nobody_qualifies_leaves_the_budget_to_the_creator(self):
        src = source([HolderEvent(0, ALICE, 10)])
        self.assertEqual(accrue(campaign(Params(gates=(WIZARD,))), src, 100, 200), {})

    def test_gate_applies_to_vault_holders_after_forwarding(self):
        # The vault holds the LP; Carol owns the vault and the Wizard.
        src = source(
            [HolderEvent(0, VAULT, 10), HolderEvent(0, ALICE, 10)],
            nfts=[HolderEvent(0, CAROL, 1)],
            wrappers=[HolderEvent(0, CAROL, 1)],
        )
        credit = accrue(campaign(Params(gates=(WIZARD,))), src, 100, 200)
        self.assertEqual(credit, {CAROL: 1000})


class TestBoosts(unittest.TestCase):
    def test_wizard_boost(self):
        # Equal LP; Alice's Wizard is worth +50%: shares 1.5 : 1.
        params = Params(boosts=(Boost(WIZARD, 5000),))
        src = source([HolderEvent(0, ALICE, 10), HolderEvent(0, BOB, 10)], nfts=[HolderEvent(0, ALICE, 1)])
        credit = accrue(campaign(params), src, 100, 200)
        self.assertEqual(credit, {ALICE: 600, BOB: 400})

    def test_tiers_stack_up_to_the_cap(self):
        tiers = (
            Boost(WIZARD, 2000),
            Boost(Requirement(Holding.NFT, WIZARDS, 3), 3000),
            Boost(Requirement(Holding.NFT, WIZARDS, 10), 20000),
        )
        params = Params(boosts=tiers, boost_cap_bps=20000)
        self.assertEqual(params.multiplier_bps((True, False, False)), 12000)
        self.assertEqual(params.multiplier_bps((True, True, False)), 15000)
        self.assertEqual(params.multiplier_bps((True, True, True)), 20000)  # capped at 2x
        src = source(
            [HolderEvent(0, ALICE, 10), HolderEvent(0, BOB, 10)],
            nfts=[HolderEvent(0, ALICE, 3), HolderEvent(0, BOB, 1)],
        )
        credit = accrue(campaign(params, amount=2700), src, 100, 200)
        self.assertEqual(credit, {ALICE: 1500, BOB: 1200})

    def test_boosts_reshare_a_fixed_budget(self):
        params = Params(boosts=(Boost(WIZARD, 10000),))
        src = source([HolderEvent(0, ALICE, 10), HolderEvent(0, BOB, 10)], nfts=[HolderEvent(0, ALICE, 1)])
        credit = accrue(campaign(params), src, 100, 200)
        self.assertEqual(sum(credit.values()), 1000)
        self.assertEqual(credit[ALICE], Fraction(2000, 3))

    def test_gate_and_boost_together(self):
        cat_boost = Boost(Requirement(Holding.CAT, SPELL, 500), 10000)
        params = Params(gates=(WIZARD,), boosts=(cat_boost,))
        src = source(
            [HolderEvent(0, ALICE, 10), HolderEvent(0, BOB, 10), HolderEvent(0, CAROL, 10)],
            nfts=[HolderEvent(0, ALICE, 1), HolderEvent(0, BOB, 1)],
            cats=[HolderEvent(0, ALICE, 500), HolderEvent(0, CAROL, 500)],
        )
        credit = accrue(campaign(params, amount=900), src, 100, 200)
        self.assertEqual(credit, {ALICE: 600, BOB: 300})  # Carol has the CAT but no Wizard


class TestTerms(unittest.TestCase):
    def test_gates_and_boosts_are_part_of_the_campaign_id(self):
        plain = campaign(Params())
        gated = campaign(Params(gates=(WIZARD,)))
        boosted = campaign(Params(boosts=(Boost(WIZARD, 5000),)))
        capped = campaign(Params(boosts=(Boost(WIZARD, 5000),), boost_cap_bps=15000))
        self.assertEqual(len({plain.id, gated.id, boosted.id, capped.id}), 4)

    def test_validation(self):
        with self.assertRaises(ValueError):
            Requirement(Holding.NFT, WIZARDS, 0)
        with self.assertRaises(ValueError):
            Boost(WIZARD, 0)
        with self.assertRaises(ValueError):
            Params(boost_cap_bps=9999)
        with self.assertRaises(ValueError):
            Params(boost_cap_bps=100001)


if __name__ == "__main__":
    unittest.main()
