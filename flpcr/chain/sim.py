"""A distributor on the in-process simulator, with one method per leaf.

Each method builds the spend through `Distributor`, adds whatever other coins
that leaf needs in the bundle (the router's or operator's message, the
creator's deposit), pushes it, and advances the driver only if the simulator
accepted it. A refused spend raises and leaves everything as it was.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from chia_wallet_sdk import Clvm, Coin, Constants, Simulator

from ..clvm import sha256tree
from ..engine import Root
from ..model import Campaign
from .driver import (
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
                 max_campaign_length: int = 365 * 4608, fee_bps: int = 300, max_campaigns: int = 32):
        self.clvm = Clvm()
        self.sim = Simulator()
        self.puzzles = Puzzles(self.clvm)
        self.keys = Keys(self.clvm)
        k = self.keys
        self.cfg = DistributorConfig(
            reward_asset=bytes(32),
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
        self.sim.spend_coins(self.d.launch_spends(fund, k.puzzle("deployer")), [])
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
        ph = self.keys.ph(label)
        coin = self.sim.new_coin(ph, 1)
        return [self.keys.spend(coin, label, [send_message_condition(message, self.d.coin.coin_id()), [51, ph, 1]])]

    def push(self, actions: Sequence[Action], extra_builders=(), slot_updates: Optional[Dict] = None):
        spends, new_state, per_action = self.d.spend(actions)
        for build in extra_builders:
            spends.extend(build())
        self.sim.spend_coins(spends, [])
        self.d.apply(new_state, slot_updates or {})
        return per_action

    def balance(self, label_or_ph) -> int:
        ph = label_or_ph if isinstance(label_or_ph, bytes) else self.keys.ph(label_or_ph)
        return sum(c.amount for c in self.sim.unspent_coins(ph, False))

    # --- leaves -------------------------------------------------------------------------

    def campaign(self, creator: str, amount: int, start: int, end: int, kind, target: bytes, **kw) -> Campaign:
        return Campaign(self.launcher_id, self.keys.ph(creator), self.cfg.reward_asset, amount, start, end, kind,
                        target, fee_bps=self.cfg.fee_bps, **kw)

    def create(self, campaign: Campaign, creator: str, router: str = "router", deposit: Optional[int] = None):
        sol = [self.h, campaign.creator, campaign.amount, campaign.start, campaign.end, int(campaign.kind),
               campaign.target, campaign.params.as_clvm(), campaign.nonce, campaign.claim_window]
        deposit = campaign.amount if deposit is None else deposit

        def router_msg():
            return self.message_from(router, b"c" + campaign.id)

        def deposit_coin():
            coin = self.sim.new_coin(self.keys.ph(creator), deposit)
            return [self.keys.spend(coin, creator, [])]

        return self.push([Action("create", sol)], [router_msg, deposit_coin])

    def post(self, root: Root, sender: str = "engine", totals: Optional[Dict[bytes, int]] = None,
             root_id: Optional[int] = None):
        totals_list = [(k, v) for k, v in sorted((root.totals if totals is None else totals).items())]
        rid = root.root_id if root_id is None else root_id
        args = [rid, root.start, root.end, root.root, totals_list, bytes(32)]
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
                     proof=None):
        cl = root.claim_for(campaign_id, ph)
        cumulative = cl.cumulative if cumulative is None else cumulative
        key = Distributor.claim_key(campaign_id, ph)
        insert, info, updates = self.d.claim_slot_info(key, cumulative)
        sol = [self.h, campaign_id, ph, cumulative, cl.proof if proof is None else proof, insert, info]
        return Action("claim", sol), updates

    def claim(self, root: Root, campaign_id: bytes, holder: str, **kw):
        ph = self.keys.ph(holder)
        before = self.balance(ph)
        action, updates = self.claim_action(root, campaign_id, ph, **kw)
        self.push([action], slot_updates=updates)
        return self.balance(ph) - before

    def reclaim(self, campaign_id: bytes):
        creator = self.d.read_state()["campaigns"][campaign_id]["creator"]
        before = self.balance(creator)
        self.push([Action("reclaim", [self.h, campaign_id])])
        return self.balance(creator) - before

    def collect(self):
        before = self.balance("treasury")
        self.push([Action("collect", [self.h])])
        return self.balance("treasury") - before

    def reserve_on_chain(self) -> int:
        state = self.sim.coin_state(self.d.reserve_coin.coin_id())
        assert state is not None and state.spent_height is None
        return state.coin.amount
