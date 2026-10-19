"""NFT collections read from the simulator: minted by a DID, owners followed
through transfers, and fed to the engine as a gate."""

import unittest

try:
    from chia_wallet_sdk import Clvm, Simulator

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


if __name__ == "__main__":
    unittest.main()
