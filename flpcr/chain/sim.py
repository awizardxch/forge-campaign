"""A distributor on the in-process simulator, with one method per leaf.

Each method asks the driver for a `LeafCall`, pushes it with `push`, and
returns what the leaf paid. `push` satisfies whatever the call needs: every
`Message` by spending a coin at the sender's address (a `Keys` label or a
registered BLS wallet), every `Signature` with the matching secret key, and a
reserved network fee from a coin that pays it. The driver advances only if the
simulator accepted the bundle; a refused spend raises and leaves everything as
it was.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Union

from chia_wallet_sdk import (
    Cat,
    CatInfo,
    CatSpend,
    Clvm,
    Coin,
    CoinSpend,
    Constants,
    LineageProof,
    Program,
    Simulator,
    Spend,
    cat_puzzle_hash,
)

from ..distributor import Config
from ..engine import Root
from ..model import Campaign
from .auth import ByAdmin, ByDelegate, ByMessage, BySignature, Message
from .driver import Distributor, LeafCall, Prepared, Puzzles
from .values import to_program

from .auth import MODE_PUZZLE_TO_ANY, MODE_PUZZLE_TO_COIN  # noqa: E402


class Keys:
    """Anyone-can-spend `(r 1)` puzzles curried with a label: distinct puzzle
    hashes standing in for the router, operator, guardian, admin, holders and
    creators. Test setup only."""

    def __init__(self, clvm: Clvm):
        self.clvm = clvm
        self.base = clvm.parse("(r 1)")
        self.labels: Dict[bytes, str] = {}

    def puzzle(self, label: str) -> Program:
        return self.base.curry([self.clvm.atom(label.encode())])

    def ph(self, label: str) -> bytes:
        ph = self.puzzle(label).tree_hash()
        self.labels[ph] = label
        return ph

    def label_of(self, ph: bytes) -> str:
        return self.labels[ph]

    def spend(self, coin: Coin, label: str, conditions: list) -> CoinSpend:
        return CoinSpend(coin, self.puzzle(label).serialize(), to_program(self.clvm, conditions).serialize())


def send_message_condition(message: bytes, receiver_coin_id: bytes, mode: int = MODE_PUZZLE_TO_COIN) -> list:
    if mode == MODE_PUZZLE_TO_ANY:  # a claim: no receiver committed
        return [66, mode, message]
    return [66, mode, message, receiver_coin_id]


Who = Union[str, object]  # a Keys label, or a `sim.bls` wallet


class SimWorld:
    def __init__(self, dispute_blocks: int = 8, min_campaign_length: int = 16,
                 max_campaign_length: int = 365 * 4608, fee_bps: int = 300, max_campaigns: int = 32,
                 reward_asset: bytes = bytes(32), rotate_delay: int = 24, delegate_fee: int = 0,
                 init: bool = True, raw_config: Optional[dict] = None, raw_bond: Optional[dict] = None):
        """`reward_asset` bytes(32) pays rewards in XCH; anything else is a CAT
        asset id, minted here with a fabricated lineage (test setup only).
        `delegate_fee` defaults to 0 here so most tests need no fee coin; the
        fee's own tests set it. `raw_config` overwrites Config fields after
        its own validation, so a probe can hand the init leaf a Config the
        Python side would refuse; `init=False` leaves the distributor
        uninitialized."""
        self.clvm = Clvm()
        self.sim = Simulator()
        self.puzzles = Puzzles(self.clvm)
        self.keys = Keys(self.clvm)
        self.wallets: Dict[bytes, object] = {}  # puzzle hash -> BLS wallet, for messages and signatures
        k = self.keys
        fund = self.sim.new_coin(k.ph("deployer"), 1)
        self.launcher_id = Coin(fund.coin_id(), Constants.singleton_launcher_hash(), 1).coin_id()
        self.cfg = Config.testnet(
            self.launcher_id, reward_asset, k.ph("dev"), k.ph("admin"), k.ph("guardian"), k.ph("router"),
            fee_bps=fee_bps, dispute_blocks=dispute_blocks, rotate_delay=rotate_delay, delegate_fee=delegate_fee,
            min_campaign_length=min_campaign_length, max_campaign_length=max_campaign_length,
            max_campaigns=max_campaigns,
        )
        for name, value in (raw_config or {}).items():
            object.__setattr__(self.cfg, name, value)
        self.engine_ph = k.ph("engine")
        self.d = Distributor(self.puzzles, self.cfg, self.engine_ph)
        if raw_bond:  # a probe: curry a malformed bond hash so only init's check can refuse it
            for name, value in raw_bond.items():
                setattr(self.d, {"bond_puzzle_hash": "bond_ph", "bond_launcher_hash": "bond_launcher_hash"}[name], value)
            config_program = to_program(self.clvm, self.d.config_value())
            from flpcr.leaves import LEAVES
            from flpcr.merkle import build_tree
            self.d.leaves = {n: self.puzzles.mods[n].curry([config_program]) for n in LEAVES}
            self.d.leaf_hashes = {n: prog.tree_hash() for n, prog in self.d.leaves.items()}
            self.d.merkle_root, self.d.leaf_proofs = build_tree([self.d.leaf_hashes[n] for n in LEAVES])
            self.d.state = to_program(self.clvm, self.d.chain_state.to_value())
        seed = self.mint_cat("deployer", 1) if self.d.is_cat else None
        self.sim.spend_coins(
            self.d.launch_spends(fund, k.puzzle("deployer"), seed, k.puzzle("deployer") if seed else None), []
        )
        if init:
            self.push([self.d.init(self.h)])

    # --- plumbing ---------------------------------------------------------------------

    @property
    def h(self) -> int:
        return self.sim.height()

    def advance_to(self, height: int) -> None:
        while self.sim.height() < height:
            self.sim.create_block()

    def bls(self, amount: int = 1):
        """A real BLS wallet, registered so `push` can spend from it and sign for it."""
        wallet = self.sim.bls(amount)
        self.wallets[wallet.puzzle_hash] = wallet
        return wallet

    def ph(self, who: Who) -> bytes:
        return self.keys.ph(who) if isinstance(who, str) else who.puzzle_hash

    def send(self, sender: bytes, message: bytes, mode: int = MODE_PUZZLE_TO_COIN) -> List[CoinSpend]:
        """A coin at `sender` sends `message` to the singleton. The coin
        recreates its own mojo at a separate change address, so the bundle
        carries no slack (a short deposit is refused rather than covered) and
        the mojo never shows up in a holder's measured payout."""
        change = self.keys.ph("change")
        condition = send_message_condition(message, self.d.coin.coin_id(), mode)
        coin = self.sim.new_coin(sender, 1)
        wallet = self.wallets.get(sender)
        if wallet is None:
            return [self.keys.spend(coin, self.keys.label_of(sender), [condition, [51, change, 1]])]
        c = self.clvm
        c.spend_standard_coin(coin, wallet.pk, c.delegated_spend(
            [to_program(c, condition), to_program(c, [51, change, 1])]))
        return list(c.coin_spends())

    def message_from(self, label: str, message: bytes) -> List[CoinSpend]:
        return self.send(self.keys.ph(label), message)

    creator_coin: Optional[Coin] = None  # the coin a `creations` entry is made from (set by `dispute`)

    def create_from(self, coin: Coin, ph: bytes, amount: int, memo: bytes) -> List[CoinSpend]:
        """`coin` (a Keys coin) creates `amount` at `ph` and keeps the rest."""
        label = self.keys.label_of(coin.puzzle_hash)
        conditions = [[51, ph, amount, [memo]]]
        if coin.amount > amount:
            conditions.append([51, coin.puzzle_hash, coin.amount - amount, [coin.puzzle_hash]])
        return [self.keys.spend(coin, label, conditions)]

    def fee_spend(self, amount: int) -> List[CoinSpend]:
        """A coin that leaves `amount` mojos as network fee."""
        payer = self.keys.ph("fee payer")
        return [self.keys.spend(self.sim.new_coin(payer, amount), "fee payer", [])]

    def push(self, calls: Sequence[LeafCall], extra_builders: Iterable = (), cat_spends: Sequence[CatSpend] = (),
             secret_keys: Iterable = (), senders: Optional[Dict[bytes, bytes]] = None) -> Prepared:
        """Build, satisfy and push one spend. `senders` swaps a required
        message's sender for another address (refusal probes: someone else
        sends what the holder should)."""
        prepared = self.d.prepare(calls, cat_spends)
        spends = list(prepared.spends)
        keys = list(secret_keys)

        def add_key(wallet):
            if wallet is not None and wallet.sk not in keys:
                keys.append(wallet.sk)

        for m in prepared.messages:
            sender = (senders or {}).get(m.sender, m.sender)
            spends.extend(self.send(sender, m.message, m.mode))
            add_key(self.wallets.get(sender))  # a wallet signs for its own coin spend
        for ph, amount, memo in prepared.creations:
            spends.extend(self.create_from(self.creator_coin, ph, amount, memo))
        if prepared.fee:
            spends.extend(self.fee_spend(prepared.fee))
        for build in extra_builders:
            spends.extend(build())
        for s in prepared.signatures:
            add_key(next((w for w in self.wallets.values() if w.pk.to_bytes() == s.public_key), None))
        self.sim.spend_coins(spends, keys)
        self.d.apply(prepared)
        return prepared

    def push_batch(self, batch) -> None:
        """Push a sequencer's `ClaimBatch`: its chained generations, a coin
        spend for each message (mode 16 needs no receiver) and every key that
        signed a claim; then advance the driver through it."""
        spends = list(batch.spends)
        keys = []
        for m in batch.messages:
            spends.extend(self.send(m.sender, m.message, m.mode))
            wallet = self.wallets.get(m.sender)
            if wallet is not None and wallet.sk not in keys:
                keys.append(wallet.sk)
        for s in batch.signatures:
            wallet = next((x for x in self.wallets.values() if x.pk.to_bytes() == s.public_key), None)
            if wallet is not None and wallet.sk not in keys:
                keys.append(wallet.sk)
        self.sim.spend_coins(spends, keys)
        self.d.apply_batch(batch)

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

    def balance(self, who) -> int:
        ph = who if isinstance(who, bytes) else self.ph(who)
        if self.d.is_cat:
            ph = cat_puzzle_hash(self.cfg.reward_asset, ph)
        return sum(c.amount for c in self.sim.unspent_coins(ph, False))

    def xch_balance(self, who) -> int:
        """Bare XCH at the address, whatever the reward asset (bonds are XCH)."""
        ph = who if isinstance(who, bytes) else self.ph(who)
        return sum(c.amount for c in self.sim.unspent_coins(ph, False))

    def reserve_on_chain(self) -> int:
        state = self.sim.coin_state(self.d.reserve_coin.coin_id())
        assert state is not None and state.spent_height is None
        return state.coin.amount

    def state(self):
        return self.d.chain_state

    # --- campaigns ------------------------------------------------------------------------

    def campaign(self, creator: str, amount: int, start: int, end: int, kind, target: bytes, **kw) -> Campaign:
        return Campaign(self.launcher_id, self.keys.ph(creator), self.cfg.reward_asset, amount, start, end, kind,
                        target, fee_bps=self.cfg.fee_bps, **kw)

    def create(self, campaign: Campaign, creator: str, router: str = "router", deposit: Optional[int] = None,
               consent: Optional[str] = None):
        return self.create_raw(
            creator, campaign.amount, campaign.start, campaign.end, int(campaign.kind), campaign.target,
            campaign.params.as_clvm(), campaign.nonce, campaign.claim_window, router=router, deposit=deposit,
            consent=consent,
        )

    def create_raw(self, creator, amount: int, start: int, end: int, kind: int, target: bytes, params: list,
                   nonce, claim_window: int, router: str = "router", deposit: Optional[int] = None,
                   h: Optional[int] = None, consent: Optional[str] = None, creator_ph: Optional[bytes] = None):
        """`create` with every field as given, including ones `Campaign` would
        refuse, for refusal probes. The router and the creator both send the id
        the leaf derives; `consent` names someone else to send the creator's
        part, `creator_ph` overrides the recorded creator (malformed lengths)."""
        ph = creator_ph if creator_ph is not None else self.ph(creator)
        call = self.d.create(self.h if h is None else h, ph, amount, start, end, kind, target, params, nonce,
                             claim_window, router=self.keys.ph(router))
        senders = {ph: self.keys.ph(consent)} if consent else None
        deposit = max(amount, 0) if deposit is None else deposit
        if self.d.is_cat:
            # The deposit sits in the reserve's CAT ring and creates nothing:
            # its value flows into the recreated reserve.
            label = creator if isinstance(creator, str) else "creator"
            cat = self.mint_cat(label, deposit)
            spend = CatSpend(cat, Spend(self.keys.puzzle(label), self.clvm.nil()))
            return self.push([call], cat_spends=[spend], senders=senders)

        def deposit_coin():
            payer = self.keys.ph("depositor")
            return [self.keys.spend(self.sim.new_coin(payer, deposit), "depositor", [])]

        return self.push([call], [deposit_coin], senders=senders)

    # --- roots and keys -----------------------------------------------------------------

    def post(self, root: Root, sender: str = None, totals: Optional[Dict[bytes, int]] = None,
             root_id: Optional[int] = None, totals_list: Optional[list] = None,
             start: Optional[int] = None, end: Optional[int] = None, h: Optional[int] = None):
        if totals_list is None:
            totals_list = [(k, v) for k, v in sorted((root.totals if totals is None else totals).items())]
        h = self.h if h is None else h
        call = self.d.post_root(h, root.root_id if root_id is None else root_id,
                                root.start if start is None else start, root.end if end is None else end,
                                root.root, totals_list, sender=self.keys.ph(sender) if sender else None)
        return self.push([call])

    def veto(self, root_id: int, sender: str = "guardian"):
        return self.push([self.d.veto(self.h, root_id, sender=self.keys.ph(sender))])

    def dispute(self, root_id: int, disputer: str = "disputer", bond: Optional[int] = None):
        """`disputer` locks the bond (default the configured amount; another
        amount for refusal probes) against the pending root."""
        amount = self.cfg.dispute_bond if bond is None else bond
        ph = self.keys.ph(disputer)
        self.creator_coin = self.sim.new_coin(ph, amount + 1)
        call = self.d.dispute(self.h, root_id, ph, self.creator_coin.coin_id())
        if bond is not None and bond != self.cfg.dispute_bond:
            # A short or long bond: the launcher is created with the wrong amount.
            call.creations = [(self.d.bond_launcher_hash, amount, self.launcher_id)]
            call.extra_spends = [self.d.bond_launcher_spend(Coin(self.creator_coin.coin_id(), self.d.bond_launcher_hash, amount))]
        return self.push([call])

    def resolve(self, upheld: int, sender: str = "guardian"):
        return self.push([self.d.resolve(self.h, upheld, sender=self.keys.ph(sender))])

    def rotate(self, new_engine: str, sender: str = "guardian"):
        return self.push([self.d.rotate(self.h, self.keys.ph(new_engine), sender=self.keys.ph(sender))])

    def cancel_rotation(self, engine: str, sender: str = "admin"):
        return self.push([self.d.cancel_rotation(self.h, self.keys.ph(engine), sender=self.keys.ph(sender))])

    # --- claims ---------------------------------------------------------------------------

    def claim_call(self, root: Root, campaign_id: bytes, ph: bytes, cumulative: Optional[int] = None,
                   proof=None, auth=ByMessage()) -> LeafCall:
        cl = root.claim_for(campaign_id, ph)
        cumulative = cl.cumulative if cumulative is None else cumulative
        return self.d.claim(self.h, campaign_id, ph, cumulative, cl.proof if proof is None else proof, auth)

    def claim(self, root: Root, campaign_id: bytes, holder: Who, sender: Optional[str] = None, **kw) -> int:
        """`holder` claims by spending a coin at its own address (`sender`
        overrides who spends, for refusal probes)."""
        ph = self.ph(holder)
        before = self.balance(ph)
        self.push([self.claim_call(root, campaign_id, ph, **kw)],
                  senders={ph: self.keys.ph(sender)} if sender else None)
        return self.balance(ph) - before

    def claim_delegated(self, root: Root, campaign_id: bytes, holder_ph: bytes, delegate: Who,
                        signed: bool = False, **kw) -> int:
        """`delegate` claims `holder_ph`'s rewards by spending a coin of its own
        (`signed` tries a bare signature instead, which the leaf refuses). They
        are paid to the holder."""
        before = self.balance(holder_ph)
        via = BySignature(delegate.pk.to_bytes()) if signed else ByMessage()
        self.push([self.claim_call(root, campaign_id, holder_ph, auth=ByDelegate(self.ph(delegate), via), **kw)])
        return self.balance(holder_ph) - before

    # --- delegation ---------------------------------------------------------------------

    def set_delegate(self, owner: Who, delegate_ph: bytes, grant: int = 1, signer: Optional[Who] = None):
        """`owner` grants (or with grant=0 revokes) `delegate_ph`. A label
        owner authorizes by message from its address; a wallet by its key.
        `signer` overrides who authorizes, for refusal probes."""
        signer = signer or owner
        owner_ph = self.ph(owner)
        if isinstance(owner, str):
            call = self.d.delegate(self.h, owner_ph, delegate_ph, grant, ByMessage())
            senders = {owner_ph: self.ph(signer)} if signer != owner else None
            return self.push([call], senders=senders)
        call = self.d.delegate(self.h, owner_ph, delegate_ph, grant, BySignature(signer.pk.to_bytes()))
        return self.push([call])

    # --- reclaim ---------------------------------------------------------------------------

    def reclaim(self, campaign_id: bytes, sender: Optional[str] = None, delegate: Optional[Who] = None,
                admin: bool = False, admin_sender: str = "admin") -> int:
        """The creator reclaims by message from its own address (`sender`
        overrides who sends it); `delegate` (a wallet, signing) reclaims as the
        creator's delegate; `admin` reclaims as the dev multisig. The payment
        always goes to the creator."""
        camp = self.state().campaign(campaign_id)
        creator = camp.creator if camp else bytes(32)
        before = self.balance(creator)
        if admin:
            call = self.d.reclaim(self.h, campaign_id, ByAdmin())
            self.push([call], senders={self.cfg.admin_ph: self.keys.ph(admin_sender)})
        elif delegate is not None:
            via = BySignature(delegate.pk.to_bytes()) if not isinstance(delegate, str) else ByMessage()
            self.push([self.d.reclaim(self.h, campaign_id, ByDelegate(self.ph(delegate), via))])
        else:
            call = self.d.reclaim(self.h, campaign_id, ByMessage())
            self.push([call], senders={creator: self.keys.ph(sender)} if sender else None)
        return self.balance(creator) - before


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

    def mint(self, owner, did, count: int, to_ph: Optional[bytes] = None,
             metadata_uris: Sequence[str] = (), metadata_hash: Optional[bytes] = None):
        """Mint `count` NFTs from `did`, delivered to `to_ph` (default the owner),
        optionally pointing at a CHIP-0007 metadata file and its hash.
        Returns (nfts, next did)."""
        from chia_wallet_sdk import NftMetadata, NftMint, TransferNft

        c = self.clvm
        to_ph = to_ph or owner.puzzle_hash
        meta = c.nft_metadata(NftMetadata(1, 1, ["https://example.invalid/wizard.png"], None, list(metadata_uris),
                                          metadata_hash, [], None))
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
