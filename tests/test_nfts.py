"""NFT collections read from the simulator: minted by a DID, owners followed
through transfers, and fed to the engine as a gate."""

import unittest

try:
    from chia_wallet_sdk import Clvm, CoinSpend, Constants, NftMetadata, NftMint, Simulator, TransferNft

    from flpcr.chain.nfts import ChainNftSource, Combined, collection, owner_history
    from flpcr.chain.sim import NftKit

    HAVE_CHAIN = True
except ImportError:  # pragma: no cover
    HAVE_CHAIN = False

from flpcr import Campaign, FixtureSource, HolderEvent, Kind, Params, accrue
from flpcr.model import Holding, Requirement

POOL, LP = b"\xb0" * 32, b"\x1b" * 32


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk missing")
class TestCollections(unittest.TestCase):
    def setUp(self):
        self.clvm, self.sim = Clvm(), Simulator()
        k = self.kit = NftKit(self.sim, self.clvm)
        self.minter, self.alice, self.bob, self.carol = (k.wallet() for _ in range(4))
        self.wizard_did = k.create_did(self.minter)
        self.wizards_id = self.wizard_did.info.launcher_id
        to_alice, did = k.mint(self.minter, self.wizard_did, 2, to_ph=self.alice.puzzle_hash)
        to_bob, did = k.mint(self.minter, did, 1, to_ph=self.bob.puzzle_hash)
        self.wizards = to_alice + to_bob
        # A look-alike: another DID mints an NFT to Carol. Not a Wizard.
        impostor = k.create_did(self.carol)
        self.fakes, _ = k.mint(self.carol, impostor, 1)
        self.minted_at = self.sim.height()

    def test_collection_is_exactly_what_the_did_minted(self):
        got = collection(self.sim, self.clvm, self.wizards_id)
        self.assertEqual(sorted(got), sorted(n.info.launcher_id for n in self.wizards))
        self.assertNotIn(self.fakes[0].info.launcher_id, got)

    def test_owners_follow_transfers(self):
        nft = self.wizards[0]
        self.kit.transfer(self.alice, nft, self.carol.puzzle_hash)
        moved_at = self.sim.height()
        history = owner_history(self.sim, self.clvm, nft.info.launcher_id)
        self.assertEqual([o for _, o in history], [self.alice.puzzle_hash, self.carol.puzzle_hash])
        self.assertLessEqual(history[-1][0], moved_at)

    def test_events_count_holdings_over_time(self):
        self.kit.transfer(self.alice, self.wizards[0], self.carol.puzzle_hash)
        src = ChainNftSource(self.sim, self.clvm)
        events = src.nft_events(self.wizards_id, self.sim.height() + 1)
        held = {}
        for e in events:
            held[e.puzzle_hash] = held.get(e.puzzle_hash, 0) + e.delta
        self.assertEqual(held[self.alice.puzzle_hash], 1)
        self.assertEqual(held[self.bob.puzzle_hash], 1)
        self.assertEqual(held[self.carol.puzzle_hash], 1)  # the Wizard Alice sent, not Carol's look-alike

    def test_gate_from_chain(self):
        """Dave has LP and no NFT; Carol has LP and only the look-alike."""
        dave = self.kit.wallet()
        lp = FixtureSource(
            lp_assets={POOL: LP},
            events={LP: [HolderEvent(0, w.puzzle_hash, 10) for w in (self.alice, self.bob, self.carol, dave)]},
        )
        src = Combined(lp, ChainNftSource(self.sim, self.clvm))
        start = self.minted_at + 1
        c = Campaign(b"\xd1" * 32, b"\xcc" * 32, b"\xca" * 32, 1000, start, start + 100, Kind.LP_HOLD, POOL,
                     Params(gates=(Requirement(Holding.NFT, self.wizards_id),)), fee_bps=0)
        credit = accrue(c, src, start, start + 100)
        self.assertEqual(credit, {self.alice.puzzle_hash: 500, self.bob.puzzle_hash: 500})


class UnreadableSpend:
    """A reader that serves one coin's spend with a solution nothing can parse:
    stands in for a melt or any spend the NFT walk cannot follow."""

    def __init__(self, reader, coin_id: bytes):
        self.reader, self.coin_id = reader, coin_id

    def coin_state(self, coin_id):
        return self.reader.coin_state(coin_id)

    def children(self, coin_id):
        return self.reader.children(coin_id)

    def coin_spend(self, coin_id):
        spend = self.reader.coin_spend(coin_id)
        if coin_id != self.coin_id or spend is None:
            return spend
        return CoinSpend(spend.coin, spend.puzzle_reveal, b"\x80")


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk missing")
class TestCollectionEvidence(unittest.TestCase):
    """FLPCR-10: an NFT counts only on evidence from a DID spend (review probe E, e_nft.py)."""

    def setUp(self):
        self.clvm, self.sim = Clvm(), Simulator()
        k = self.kit = NftKit(self.sim, self.clvm)
        self.minter, self.mallory, self.alice = k.wallet(), k.wallet(), k.wallet()
        self.did = k.create_did(self.minter)
        self.wizards_id = self.did.info.launcher_id
        self.real, self.did = k.mint(self.minter, self.did, 1, to_ph=self.alice.puzzle_hash)

    def meta(self):
        return self.clvm.nft_metadata(NftMetadata(1, 1, ["x"], None, [], None, [], None))

    def did_spend(self, extra):
        c = self.clvm
        recreate = c.create_coin(self.did.info.inner_puzzle_hash(), 1, c.list([c.atom(self.minter.puzzle_hash)]))
        return c.spend_did(self.did, c.standard_spend(self.minter.pk, c.delegated_spend([recreate, *extra])))

    def test_control(self):
        self.assertEqual(collection(self.sim, self.clvm, self.wizards_id), [self.real[0].info.launcher_id])

    def test_launcher_under_a_did_payment_is_not_counted(self):
        """Case A: a DID spend pays Mallory a coin; she launches her own NFT from it."""
        c, sim = self.clvm, self.sim
        gift_parent = self.did.coin.coin_id()
        self.did = self.did_spend([c.create_coin(self.mallory.puzzle_hash, 0, None)])
        sim.spend_coins(c.coin_spends(), [self.minter.sk])
        gift = next(s.coin for s in sim.children(gift_parent) if s.coin.puzzle_hash == self.mallory.puzzle_hash)
        m = c.mint_nfts(gift.coin_id(), [NftMint(self.meta(), Constants.nft_metadata_updater_default_hash(),
                                                 self.mallory.puzzle_hash, self.mallory.puzzle_hash, 0, None)])
        c.spend_standard_coin(gift, self.mallory.pk, c.delegated_spend(m.parent_conditions))
        fund = sim.new_coin(self.mallory.puzzle_hash, 1)
        c.spend_standard_coin(fund, self.mallory.pk, c.delegated_spend([]))
        sim.spend_coins(c.coin_spends(), [self.mallory.sk])
        # Before the fix: counted, and Mallory held one Wizard.
        got = collection(sim, c, self.wizards_id)
        self.assertNotIn(m.nfts[0].info.launcher_id, got)
        self.assertEqual(got, [self.real[0].info.launcher_id])
        events = ChainNftSource(sim, c).nft_events(self.wizards_id, sim.height() + 1)
        self.assertFalse([e for e in events if e.puzzle_hash == self.mallory.puzzle_hash])

    def test_did_authorized_mint_from_an_xch_coin_is_counted(self):
        """Case B: the launcher's parent is an XCH coin; the DID announces it and becomes its owner."""
        c, sim = self.clvm, self.sim
        fund = sim.new_coin(self.minter.puzzle_hash, 1)
        m = c.mint_nfts(fund.coin_id(), [NftMint(
            self.meta(), Constants.nft_metadata_updater_default_hash(), self.alice.puzzle_hash,
            self.minter.puzzle_hash, 300, TransferNft(self.wizards_id, [], self.did.info.inner_puzzle_hash()))])
        c.spend_standard_coin(fund, self.minter.pk, c.delegated_spend(m.parent_conditions))
        launcher_id = m.nfts[0].info.launcher_id
        self.did = self.did_spend([c.create_puzzle_announcement(launcher_id)])
        sim.spend_coins(c.coin_spends(), [self.minter.sk])
        # Before the fix: not counted, so a real holder failed the gate.
        got = collection(sim, c, self.wizards_id)
        self.assertEqual(sorted(got), sorted([self.real[0].info.launcher_id, launcher_id]))

    def test_did_owner_set_later_is_not_counted(self):
        """An NFT minted elsewhere, eve not owned by the DID: an announcement of
        its launcher id in a later DID spend does not make it a Wizard."""
        c, sim = self.clvm, self.sim
        fund = sim.new_coin(self.mallory.puzzle_hash, 1)
        m = c.mint_nfts(fund.coin_id(), [NftMint(self.meta(), Constants.nft_metadata_updater_default_hash(),
                                                 self.mallory.puzzle_hash, self.mallory.puzzle_hash, 0, None)])
        c.spend_standard_coin(fund, self.mallory.pk, c.delegated_spend(m.parent_conditions))
        sim.spend_coins(c.coin_spends(), [self.mallory.sk])
        launcher_id = m.nfts[0].info.launcher_id
        self.did = self.did_spend([c.create_puzzle_announcement(launcher_id)])
        sim.spend_coins(c.coin_spends(), [self.minter.sk])
        self.assertNotIn(launcher_id, collection(sim, c, self.wizards_id))


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk missing")
class TestNftEnds(unittest.TestCase):
    """Review E (NFT melt): an NFT the walk cannot follow stops counting for its last owner."""

    def test_unreadable_spend_ends_the_holding(self):
        clvm, sim = Clvm(), Simulator()
        k = NftKit(sim, clvm)
        minter, alice, bob = k.wallet(), k.wallet(), k.wallet()
        did = k.create_did(minter)
        (n1, n2), _ = k.mint(minter, did, 2, to_ph=alice.puzzle_hash)
        k.transfer(alice, n2, bob.puzzle_hash)
        spent_at = sim.coin_state(n2.coin.coin_id()).spent_height
        reader = UnreadableSpend(sim, n2.coin.coin_id())

        history = owner_history(reader, clvm, n2.info.launcher_id)
        self.assertEqual(history[-1], (spent_at, None))
        before = sim.height() + 1000
        held = {}
        for e in ChainNftSource(reader, clvm).nft_events(did.info.launcher_id, before):
            held[e.puzzle_hash] = held.get(e.puzzle_hash, 0) + e.delta
        # Before the fix: Alice kept counting n2 forever.
        self.assertEqual(held.get(alice.puzzle_hash), 1)
        self.assertFalse(held.get(bob.puzzle_hash))
        # The honest reader follows the transfer as before.
        self.assertEqual([o for _, o in owner_history(sim, clvm, n2.info.launcher_id)],
                         [alice.puzzle_hash, bob.puzzle_hash])


if __name__ == "__main__":
    unittest.main()
