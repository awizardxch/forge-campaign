"""A distributor on the in-process simulator, with one method per leaf.

Each method builds the spend through `Distributor`, adds whatever other coins
that leaf needs in the bundle (the router's or operator's message, the
creator's deposit), pushes it, and advances the driver only if the simulator
accepted it. A refused spend raises and leaves everything as it was.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from chia_wallet_sdk import Cat, CatInfo, CatSpend, Clvm, Coin, Constants, LineageProof, Simulator, Spend, cat_puzzle_hash

from ..clvm import sha256tree
from ..engine import Root
from ..merkle import claim_leaf
from ..model import Campaign
from .driver import (
    AUTH_DELEGATE,
    AUTH_MESSAGE,
    AUTH_SIGNATURE,
    HIGH,
    LOW,
    Action,
    Distributor,
    DistributorConfig,
    Keys,
    Puzzles,
    send_message_condition,
)


class SimWorld:
    def __init__(self, dispute_blocks: int = 8, min_campaign_length: int = 16,
                 max_campaign_length: int = 365 * 4608, fee_bps: int = 300, max_campaigns: int = 32,
                 reward_asset: bytes = bytes(32)):
        """`reward_asset` bytes(32) pays rewards in XCH; anything else is a CAT
        asset id, minted here with a fabricated lineage (test setup only)."""
        self.clvm = Clvm()
        self.sim = Simulator()
        self.puzzles = Puzzles(self.clvm)
        self.keys = Keys(self.clvm)
        k = self.keys
        self.cfg = DistributorConfig(
            reward_asset=reward_asset,
            fee_bps=fee_bps,
            treasury_ph=k.ph("treasury"),
            guardian_ph=k.ph("guardian"),
            router_ph=k.ph("router"),
            engine_ph=k.ph("engine"),
            dispute_blocks=dispute_blocks,
            min_campaign_length=min_campaign_length,
            max_campaign_length=max_campaign_length,
            max_campaigns=max_campaigns,
        )
        fund = self.sim.new_coin(k.ph("deployer"), 1)
        self.launcher_id = Coin(fund.coin_id(), Constants.singleton_launcher_hash(), 1).coin_id()
        self.d = Distributor(self.puzzles, self.cfg, self.launcher_id)
        seed = self.mint_cat("deployer", 1) if self.d.is_cat else None
        self.sim.spend_coins(
            self.d.launch_spends(fund, k.puzzle("deployer"), seed, k.puzzle("deployer") if seed else None), []
        )
        self.push([Action("init", [self.h])], slot_updates={LOW: (0, LOW, HIGH), HIGH: (0, LOW, HIGH)})

    # --- plumbing ---------------------------------------------------------------------

    @property
    def h(self) -> int:
        return self.sim.height()

    def advance_to(self, height: int) -> None:
        while self.sim.height() < height:
            self.sim.create_block()

    def message_from(self, label: str, message: bytes) -> list:
        # The sender recreates its own mojo, so the bundle carries no slack and
        # a short deposit is refused instead of being covered by this coin.
        # Its mojo goes to a separate change address so it never shows up in
        # a holder's measured payout.
        ph = self.keys.ph(label)
        coin = self.sim.new_coin(ph, 1)
        change = self.keys.ph("change")
        return [self.keys.spend(coin, label, [send_message_condition(message, self.d.coin.coin_id()), [51, change, 1]])]

    def push(self, actions: Sequence[Action], extra_builders=(), slot_updates: Optional[Dict] = None,
             cat_spends=(), secret_keys=(), delegation_updates: Optional[Dict] = None):
        spends, new_state, per_action = self.d.spend(actions, cat_spends)
        for build in extra_builders:
            spends.extend(build())
        self.sim.spend_coins(spends, list(secret_keys))
        self.d.apply(new_state, slot_updates or {}, delegation_updates)
        return per_action

    def mint_cat(self, label: str, amount: int) -> Cat:
        """A coin of the reward CAT owned by `label`. The parent never existed;
        consensus only checks the lineage arithmetic, which is all a test needs."""
        asset = self.cfg.reward_asset
        ph = self.keys.ph(label)
        info = CatInfo(asset, None, ph)
        fake_grandparent = self.clvm.atom(label.encode() + amount.to_bytes(8, "big")).tree_hash()
        parent = Coin(fake_grandparent, info.puzzle_hash(), amount)
        coin = Coin(parent.coin_id(), info.puzzle_hash(), amount)
        self.sim.insert_coin(coin)
        return Cat(coin, LineageProof(fake_grandparent, ph, amount), info)

    def balance(self, label_or_ph) -> int:
        ph = label_or_ph if isinstance(label_or_ph, bytes) else self.keys.ph(label_or_ph)
        if self.d.is_cat:
            ph = cat_puzzle_hash(self.cfg.reward_asset, ph)
        return sum(c.amount for c in self.sim.unspent_coins(ph, False))

    # --- leaves -------------------------------------------------------------------------

    def campaign(self, creator: str, amount: int, start: int, end: int, kind, target: bytes, **kw) -> Campaign:
        return Campaign(self.launcher_id, self.keys.ph(creator), self.cfg.reward_asset, amount, start, end, kind,
                        target, fee_bps=self.cfg.fee_bps, **kw)

    def create(self, campaign: Campaign, creator: str, router: str = "router", deposit: Optional[int] = None):
        return self.create_raw(
            creator, campaign.amount, campaign.start, campaign.end, int(campaign.kind), campaign.target,
            campaign.params.as_clvm(), campaign.nonce, campaign.claim_window, router=router, deposit=deposit,
        )

    def create_raw(self, creator: str, amount: int, start: int, end: int, kind: int, target: bytes, params: list,
                   nonce, claim_window: int, router: str = "router", deposit: Optional[int] = None,
                   h: Optional[int] = None):
        """`create` with every field as given, including ones `Campaign` would
        refuse, for refusal probes. The router signs the id the leaf derives."""
        creator_ph = self.keys.ph(creator)
        sol = [self.h if h is None else h, creator_ph, amount, start, end, kind, target, params, nonce, claim_window]
        campaign_id = sha256tree([
            "flpcr-campaign-v1", self.launcher_id, creator_ph, self.cfg.reward_asset, amount, start, end, kind,
            target, sha256tree(params), nonce, self.cfg.fee_bps, claim_window,
        ])
        deposit = max(amount, 0) if deposit is None else deposit

        def router_msg():
            return self.message_from(router, b"c" + campaign_id)

        if self.d.is_cat:
            # The deposit sits in the reserve's CAT ring and creates nothing:
            # its value flows into the recreated reserve.
            cat = self.mint_cat(creator, deposit)
            spend = CatSpend(cat, Spend(self.keys.puzzle(creator), self.clvm.nil()))
            return self.push([Action("create", sol)], [router_msg], cat_spends=[spend])

        def deposit_coin():
            coin = self.sim.new_coin(self.keys.ph(creator), deposit)
            return [self.keys.spend(coin, creator, [])]

        return self.push([Action("create", sol)], [router_msg, deposit_coin])

    def post(self, root: Root, sender: str = "engine", totals: Optional[Dict[bytes, int]] = None,
             root_id: Optional[int] = None, totals_list: Optional[list] = None,
             start: Optional[int] = None, end: Optional[int] = None):
        if totals_list is None:
            totals_list = [(k, v) for k, v in sorted((root.totals if totals is None else totals).items())]
        rid = root.root_id if root_id is None else root_id
        args = [rid, root.start if start is None else start, root.end if end is None else end, root.root,
                totals_list, bytes(32)]
        return self.push(
            [Action("post_root", [self.h, *args])],
            [lambda: self.message_from(sender, b"p" + sha256tree(args))],
        )

    def veto(self, root_id: int, sender: str = "guardian"):
        return self.push(
            [Action("veto", [self.h, root_id])],
            [lambda: self.message_from(sender, b"v" + sha256tree(root_id))],
        )

    def rotate(self, new_engine: str, sender: str = "guardian"):
        ph = self.keys.ph(new_engine)
        return self.push(
            [Action("rotate", [self.h, ph])],
            [lambda: self.message_from(sender, b"r" + ph)],
        )

    def claim_action(self, root: Root, campaign_id: bytes, ph: bytes, cumulative: Optional[int] = None,
                     proof=None, auth=AUTH_MESSAGE, auth_data=None):
        """The claim action. `auth` is how the holder authorizes it:
        AUTH_MESSAGE (a coin at the holder's address sends "k" + leaf; add
        `holder_message`) or AUTH_SIGNATURE (auth_data = the holder's
        synthetic public key; push with the holder's secret key)."""
        cl = root.claim_for(campaign_id, ph)
        cumulative = cl.cumulative if cumulative is None else cumulative
        key = Distributor.claim_key(campaign_id, ph)
        insert, info, updates = self.d.claim_slot_info(key, cumulative)
        sol = [self.h, campaign_id, ph, cumulative, cl.proof if proof is None else proof, insert, info, auth,
               auth_data]
        return Action("claim", sol), updates

    def holder_message(self, label: str, campaign_id: bytes, ph: bytes, cumulative: int):
        """Builder for the AUTH_MESSAGE spend: `label`'s coin sends the claim
        leaf. Only a coin at the holder's own puzzle hash satisfies the leaf."""
        leaf = claim_leaf(self.launcher_id, campaign_id, ph, cumulative)
        return lambda: self.message_from(label, b"k" + leaf)

    def claim(self, root: Root, campaign_id: bytes, holder: str, sender: Optional[str] = None, **kw):
        """`holder` claims with a message from its own address (`sender`
        overrides who sends it, for refusal probes)."""
        ph = self.keys.ph(holder)
        before = self.balance(ph)
        action, updates = self.claim_action(root, campaign_id, ph, **kw)
        cumulative = action.solution[3]
        self.push([action], [self.holder_message(sender or holder, campaign_id, ph, cumulative)],
                  slot_updates=updates)
        return self.balance(ph) - before

    # --- delegation: a cold wallet lets a hot wallet claim for it ----------------------

    def delegation_message(self, holder_ph: bytes, delegate_ph: bytes, grant: int) -> bytes:
        return sha256tree(["flpcr-delegate-v1", self.launcher_id, holder_ph, delegate_ph, grant])

    def set_delegate(self, holder, delegate_ph: bytes, grant: int = 1, signer=None):
        """`holder` grants (or with grant=0 revokes) `delegate_ph`. A str holder
        authorizes by message from its address; a `sim.bls` wallet by its key.
        `signer` overrides who authorizes, for refusal probes."""
        signer = signer or holder
        if isinstance(holder, str):
            holder_ph = self.keys.ph(holder)
            sol = [self.h, holder_ph, delegate_ph, grant, AUTH_MESSAGE, None]
            msg = self.delegation_message(holder_ph, delegate_ph, grant)
            builders = [lambda: self.message_from(signer, b"k" + msg)]
            keys = []
        else:
            holder_ph = holder.puzzle_hash
            sol = [self.h, holder_ph, delegate_ph, grant, AUTH_SIGNATURE, signer.pk.to_bytes()]
            builders, keys = [], [signer.sk]
        self.push([Action("delegate", sol)], builders, secret_keys=keys,
                  delegation_updates={(holder_ph, delegate_ph): bool(grant)})

    def claim_delegated(self, root: Root, campaign_id: bytes, holder_ph: bytes, delegate, **kw):
        """`delegate` (a `sim.bls` hot wallet, or a str label authorizing by
        message) claims `holder_ph`'s rewards. They are paid to the holder."""
        before = self.balance(holder_ph)
        if isinstance(delegate, str):
            delegate_ph = self.keys.ph(delegate)
            data = (delegate_ph, (AUTH_MESSAGE, None))
            keys = []
        else:
            delegate_ph = delegate.puzzle_hash
            data = (delegate_ph, (AUTH_SIGNATURE, delegate.pk.to_bytes()))
            keys = [delegate.sk]
        action, updates = self.claim_action(root, campaign_id, holder_ph, auth=AUTH_DELEGATE, auth_data=data, **kw)
        builders = []
        if isinstance(delegate, str):
            builders = [self.holder_message(delegate, campaign_id, holder_ph, action.solution[3])]
        self.push([action], builders, slot_updates=updates, secret_keys=keys,
                  delegation_updates={(holder_ph, delegate_ph): True})
        return self.balance(holder_ph) - before

    def claim_signed(self, root: Root, campaign_id: bytes, holder, signer=None, **kw):
        """`holder` (a `sim.bls` wallet) claims with its key's AGG_SIG_ME.
        `signer` overrides whose key signs and is named, for refusal probes."""
        signer = signer or holder
        ph = holder.puzzle_hash
        before = self.balance(ph)
        action, updates = self.claim_action(root, campaign_id, ph, auth=AUTH_SIGNATURE,
                                            auth_data=signer.pk.to_bytes(), **kw)
        self.push([action], slot_updates=updates, secret_keys=[signer.sk])
        return self.balance(ph) - before

    def reclaim_message(self, campaign_id: bytes) -> bytes:
        return sha256tree(["flpcr-reclaim-v1", self.launcher_id, campaign_id])

    def reclaim(self, campaign_id: bytes, sender: Optional[str] = None, delegate=None, auth: Optional[int] = None):
        """Only the creator reclaims. By default the creator's own address
        sends the message; `sender` overrides that (refusal probes);
        `delegate` (a `sim.bls` wallet) reclaims as the creator's delegate."""
        creator = self.d.read_state()["campaigns"][campaign_id]["creator"]
        before = self.balance(creator)
        msg = self.reclaim_message(campaign_id)
        if delegate is not None:
            sol = [self.h, campaign_id, AUTH_DELEGATE,
                   (delegate.puzzle_hash, (AUTH_SIGNATURE, delegate.pk.to_bytes()))]
            self.push([Action("reclaim", sol)], secret_keys=[delegate.sk],
                      delegation_updates={(creator, delegate.puzzle_hash): True})
        else:
            label = sender or self.keys.label_of(creator)
            sol = [self.h, campaign_id, AUTH_MESSAGE if auth is None else auth, None]
            self.push([Action("reclaim", sol)], [lambda: self.message_from(label, b"k" + msg)])
        return self.balance(creator) - before

    def collect(self):
        before = self.balance("treasury")
        self.push([Action("collect", [self.h])])
        return self.balance("treasury") - before

    def reserve_on_chain(self) -> int:
        state = self.sim.coin_state(self.d.reserve_coin.coin_id())
        assert state is not None and state.spent_height is None
        return state.coin.amount


class NftKit:
    """DIDs and NFTs on the simulator, owned by real BLS keys (`sim.bls`), for
    tests of collection gating. Mirrors what a wallet does: the DID's own
    spend creates the NFT launchers, and each NFT's eve spend sets the DID as
    its owner."""

    def __init__(self, sim, clvm):
        self.sim, self.clvm = sim, clvm

    def wallet(self, amount: int = 1000):
        return self.sim.bls(amount)

    def create_did(self, owner):
        created = self.clvm.create_eve_did(owner.coin.coin_id(), owner.puzzle_hash)
        self.clvm.spend_standard_coin(owner.coin, owner.pk, self.clvm.delegated_spend(created.parent_conditions))
        self.sim.spend_coins(self.clvm.coin_spends(), [owner.sk])
        return created.did

    def mint(self, owner, did, count: int, to_ph: Optional[bytes] = None):
        """Mint `count` NFTs from `did`, delivered to `to_ph` (default the owner).
        Returns (nfts, next did)."""
        from chia_wallet_sdk import NftMetadata, NftMint, TransferNft

        c = self.clvm
        to_ph = to_ph or owner.puzzle_hash
        meta = c.nft_metadata(NftMetadata(1, 1, ["https://example.invalid/wizard.png"], None, [], None, [], None))
        mints = [
            NftMint(meta, Constants.nft_metadata_updater_default_hash(), to_ph, owner.puzzle_hash, 300,
                    TransferNft(did.info.launcher_id, [], did.info.inner_puzzle_hash()))
            for _ in range(count)
        ]
        minted = c.mint_nfts(did.coin.coin_id(), mints)
        recreate = c.create_coin(did.info.inner_puzzle_hash(), 1, c.list([c.atom(owner.puzzle_hash)]))
        nxt = c.spend_did(did, c.standard_spend(owner.pk, c.delegated_spend([recreate, *minted.parent_conditions])))
        fund = self.sim.new_coin(owner.puzzle_hash, 2 * count + 1)
        c.spend_standard_coin(fund, owner.pk, c.delegated_spend([]))
        self.sim.spend_coins(c.coin_spends(), [owner.sk])
        return list(minted.nfts), nxt

    def transfer(self, owner, nft, to_ph: bytes):
        c = self.clvm
        moved = c.spend_nft(nft, c.standard_spend(owner.pk, c.delegated_spend(
            [c.create_coin(to_ph, 1, c.list([c.atom(to_ph)]))]
        )))
        self.sim.spend_coins(c.coin_spends(), [owner.sk])
        return moved
