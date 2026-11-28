"""A campaign's creator takes back what its holders left (owner, 2026-11-27: "unclaimed rewards
after a campaign ends should have about 48hrs then the deployer can claim it back and would
close everything").

The reclaim leaf takes the creator's word as a mode-23 message, `"k" + tree_hash(["flpcr-reclaim-v1",
launcher, campaign_id])`, from a coin at the creator's address to the singleton's CURRENT coin, and
pays the creator whatever holders left; the campaign is then removed, which frees its slot. So it
works like a create (flpcr.service.intake):

  POST /api/v1/reclaim/quote  {campaign}            checks the leaf would take it now, takes an
                                                     open lease on the singleton coin, and answers
                                                     the message, the coin it names and the
                                                     network fee the creator's spend must reserve
  POST /api/v1/reclaim        {quote, spend_bundle} the creator's signed spend (its own coin: the
                                                     message, the change, RESERVE_FEE): the service
                                                     adds the singleton's spend and pushes both

The creator pays the network fee; the service holds no key for this and signs nothing. A quote
names one singleton coin: anything else that spends it first (a claim, a root) makes the
creator's message stale, and the creator re-quotes.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from chia_wallet_sdk import Clvm

from ..chain.auth import ByMessage
from ..chain.driver import HEIGHT_WINDOW
from ..chain.signer import MAX_COST
from ..chain.values import to_bytes, to_int, to_list
from ..clvm import sha256tree
from .chain import PushRefused
from .core import FEE_PER_COST, MIN_FEE
from .lease import LEASE_SECONDS, LeaseBusy

# The creator's own spend beside the singleton's, allowed for when sizing the fee.
CREATOR_SPEND_COST = 30_000_000
H_MARGIN = 6  # as intake.H_MARGIN: re-quote before the quoted height leaves the leaf's window
MAX_QUOTES = 256


class Requote(ValueError):
    """The quote can no longer be used; ask for a new one (HTTP 409, `requote`)."""


@dataclass
class ReclaimQuote:
    id: str
    campaign_id: bytes
    creator: bytes
    coin_id: bytes
    h: int
    message: bytes
    network_fee: int
    amount: int
    at: float
    status: str = "quoted"
    detail: Optional[str] = None
    height: Optional[int] = None


def reclaim_message(launcher_id: bytes, campaign_id: bytes) -> bytes:
    """What the creator's coin sends: `"k" + tree_hash(["flpcr-reclaim-v1", launcher, campaign])`."""
    return b"k" + sha256tree(["flpcr-reclaim-v1", launcher_id, campaign_id])


def _reserved_and_messages(spends) -> tuple:
    """(RESERVE_FEE total, [(mode, message, sender ph, receiver)]) over `spends`, run as the chain runs them."""
    clvm, reserved, sent = Clvm(), 0, []
    for s in spends:
        ran = clvm.deserialize(s.puzzle_reveal).run(clvm.deserialize(s.solution), MAX_COST, False)
        for cond in to_list(ran.value):
            items = to_list(cond) if cond.is_pair() else []
            if not items or not items[0].is_atom():
                continue
            op = to_int(items[0])
            if op == 52 and len(items) >= 2:
                reserved += to_int(items[1])
            elif op == 66 and len(items) >= 4:
                sent.append((to_int(items[1]), to_bytes(items[2]), s.coin.puzzle_hash, to_bytes(items[3])))
    return reserved, sent


class ReclaimDesk:
    def __init__(self, service, lease_seconds: float = LEASE_SECONDS):
        self.service = service
        self.lease_seconds = lease_seconds
        self.quotes: Dict[str, ReclaimQuote] = {}

    def quote(self, body: dict) -> dict:
        svc = self.service
        raw = bytes.fromhex(str(body.get("campaign") or body.get("campaign_id") or "").removeprefix("0x"))
        if len(raw) != 32:
            raise ValueError("campaign is 32 bytes of hex")
        if not svc.write_lock.acquire(timeout=10):
            raise LeaseBusy("the distributor is being spent; retry shortly", 20)
        try:
            svc.follow()
            d, h = svc.d, svc.chain.peak()
            camp = svc.reclaim_ready(d.chain_state, h, raw)
            call = d.reclaim(h, raw, ByMessage())
            coin_id = d.coin.coin_id()
            mark = d._mark()
            try:
                prepared = d.prepare([call])
                cost = d.spend_cost(list(prepared.spends))
            finally:
                d._rewind(mark)
            fee = max(MIN_FEE, FEE_PER_COST * (cost + CREATOR_SPEND_COST))
            if len(self.quotes) >= MAX_QUOTES:  # forget the oldest finished ones
                for k in sorted(self.quotes, key=lambda k: self.quotes[k].at)[: MAX_QUOTES // 2]:
                    self.quotes.pop(k, None)
            q = ReclaimQuote("r" + secrets.token_hex(12), raw, camp.creator, coin_id, h,
                             reclaim_message(d.launcher_id, raw), fee, camp.budget - camp.claimed, time.time())
            svc.lease.take(q.id, coin_id, self.lease_seconds)
            self.quotes[q.id] = q
        finally:
            svc.write_lock.release()
        return {"quote": q.id, "campaign": raw.hex(), "creator": q.creator.hex(), "coin_id": coin_id.hex(),
                "message": q.message.hex(), "mode": 23, "network_fee": fee, "amount": q.amount,
                "expires_in": int(self.lease_seconds), "height": h}

    def submit(self, body: dict, wait: float = 1800, poll: float = 15) -> dict:
        from .sequencer import _bundle

        svc = self.service
        q = self.quotes.get(str(body.get("quote") or ""))
        if q is None:
            raise Requote("unknown or expired reclaim quote; re-quote")
        if q.status != "quoted":
            raise ValueError(f"this reclaim is already {q.status}")
        if svc.lease.held_by(q.id) is None:
            raise Requote("the reclaim quote lapsed; re-quote and sign again")
        if svc.lease.pinned():
            raise LeaseBusy("another spend of this distributor is being confirmed; retry shortly", 30)
        spends, sig = _bundle(Clvm(), body.get("spend_bundle"))
        if not spends:
            raise ValueError("the creator's spend is missing")
        reserved, sent = _reserved_and_messages(spends)
        if not any(m == (23, q.message, q.creator, q.coin_id) for m in sent):
            raise ValueError("the creator's spend does not send this reclaim's message from the creator's address "
                             "to the quoted distributor coin")
        if reserved < q.network_fee:
            raise ValueError(f"the creator's spend reserves {reserved:,} mojos; this reclaim needs {q.network_fee:,}")
        if not svc.write_lock.acquire(timeout=10):
            raise LeaseBusy("the distributor is being spent; retry shortly", 20)
        try:
            d = svc.d
            if d.coin.coin_id() != q.coin_id or svc.chain.spent(q.coin_id):
                svc.lease.release(q.id)
                svc.follow()
                raise Requote("the distributor moved since the quote; re-quote and sign again")
            h = svc.chain.peak()
            if h >= q.h + HEIGHT_WINDOW - H_MARGIN:
                svc.lease.release(q.id)
                raise Requote("the quote's height is too old for the reclaim spend; re-quote")
            svc.reclaim_ready(d.chain_state, h, q.campaign_id)
            prepared = d.prepare([d.reclaim(q.h, q.campaign_id, ByMessage())])
            successor = d.successor_coin(prepared)
            svc.lease.pin(q.id)
            try:
                svc.chain.push(list(prepared.spends) + list(spends), sig)
            except PushRefused as e:
                svc.lease.release(q.id)
                q.status, q.detail = "failed", str(e)
                raise ValueError(f"the chain refused the reclaim: {e}") from e
            q.status = "pushed"
        finally:
            svc.write_lock.release()
        try:
            q.height = svc._wait(successor.coin_id(), wait, poll)
        except RuntimeError as e:
            q.status, q.detail = "failed", f"not confirmed: {e}"
            svc.lease.release(q.id)
            svc.boot()
            raise ValueError(f"the reclaim was pushed but not confirmed: {e}") from e
        svc.lease.release(q.id)
        svc.follow()  # the reclaim is now the chain's: follow takes it in, as it does a lock's
        q.status = "confirmed"
        return {"quote": q.id, "campaign": q.campaign_id.hex(), "status": q.status, "height": q.height,
                "amount": q.amount, "network_fee": q.network_fee}
