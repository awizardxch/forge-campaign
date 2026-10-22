"""The claim sequencer: holders submit signed claims; each batch pays many of them in one bundle.

A request is everything one claimant brings:

    {"claims": [{"campaign": hex, "address": xch1.. | "puzzle_hash": hex,
                 "public_key": hex}],            # key claims: the address's synthetic key
     "signature": hex,                            # key claims: aggregate AGG_SIG_UNSAFE over the leaves
     "spend_bundle": {"coin_spends": [...], "aggregated_signature": hex}}

* Key claims (wallets): `signature` signs every claim leaf (the one-approval stand-in spend
  Forge's front end already uses), and `spend_bundle` is a small XCH spend of any of the
  claimant's coins that pays the fee: the claimant pays the transaction.
* Message claims (locks, multisigs, vaults): no `public_key`; `spend_bundle`'s spends send
  "k" + leaf in mode 16 from the claimed address and pay the fee. A lock's executed proposal
  is exactly such a bundle.

Everything is checked before a request is queued: the key hashes to the address, the
signatures verify, the messages are there, the coins are unspent, and the fee covers the
request's share of the bundle's cost. A bad request is refused on its own; it never sinks a
batch.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from chia_wallet_sdk import Clvm, Coin, CoinSpend, PublicKey, Signature, standard_puzzle_hash

from ..chain.auth import BySignature, ByMessage
from ..chain.driver import ClaimRequest
from ..chain.signer import ADDITIONAL_DATA, AGG_SIG_ME, AGG_SIG_UNSAFE, MAX_COST
from ..chain.values import to_bytes, to_int, to_list
from .chain import PushRefused
from .http import puzzle_hash_of

FEE_PER_CLAIM = 600_000_000  # 15 x the ~40M cost of a first claim: clear of the 5 mojo/cost floor
MAX_REQUEST_CLAIMS = 32


@dataclass
class Pending:
    id: str
    claims: List[ClaimRequest]
    signature: Signature  # key claims' leaf signatures, aggregated with the bundle's
    spends: List[CoinSpend]
    fee: int
    status: str = "queued"
    detail: Optional[str] = None
    height: Optional[int] = None
    at: float = field(default_factory=time.time)


def _bundle(clvm: Clvm, body: Optional[dict]):
    if not body:
        return [], Signature.infinity()
    spends = []
    for s in body.get("coin_spends") or []:
        c = s["coin"]
        hx = lambda v: bytes.fromhex(str(v).removeprefix("0x"))  # noqa: E731
        coin = Coin(hx(c["parent_coin_info"]), hx(c["puzzle_hash"]), int(c["amount"]))
        spends.append(CoinSpend(coin, hx(s["puzzle_reveal"]), hx(s["solution"])))
    sig = Signature.from_bytes(bytes.fromhex(str(body.get("aggregated_signature", "")).removeprefix("0x")))
    return spends, sig


class ClaimSequencer:
    def __init__(self, service, fee_per_claim: int = FEE_PER_CLAIM):
        self.service = service
        self.fee_per_claim = fee_per_claim
        self.queue: Dict[str, Pending] = {}
        self.lock = threading.Lock()

    # --- intake ---------------------------------------------------------------------------

    def submit(self, body: dict) -> dict:
        clvm = Clvm()
        svc = self.service
        claims = body.get("claims") or []
        if not claims or len(claims) > MAX_REQUEST_CLAIMS:
            raise ValueError(f"a request carries 1 to {MAX_REQUEST_CLAIMS} claims")
        spends, bundle_sig = _bundle(clvm, body.get("spend_bundle"))
        conds = self._conditions(clvm, spends)
        pks, msgs = [], []
        requests: List[ClaimRequest] = []
        seen = set()
        for item in claims:
            ph = puzzle_hash_of(item.get("address") or item.get("puzzle_hash") or "")
            cid = bytes.fromhex(str(item.get("campaign", "")).removeprefix("0x"))
            if (cid, ph) in seen:
                raise ValueError("a claim appears twice in one request")
            seen.add((cid, ph))
            leaf = next((x for x in svc.claimable(ph)["claims"] if x["campaign"] == cid.hex()), None)
            if leaf is None or leaf["claimable"] <= 0:
                raise ValueError(f"nothing to claim for {ph.hex()[:12]}… in campaign {cid.hex()[:12]}…")
            proof = (leaf["proof"][0], [bytes.fromhex(x) for x in leaf["proof"][1]])
            message = bytes.fromhex(leaf["leaf"])
            if item.get("public_key"):
                pk = PublicKey.from_bytes(bytes.fromhex(str(item["public_key"]).removeprefix("0x")))
                if standard_puzzle_hash(pk) != ph:
                    raise ValueError(f"public key does not own {ph.hex()[:12]}…")
                pks.append(pk)
                msgs.append(message)
                auth = BySignature(pk.to_bytes())
            else:
                want = b"k" + message
                if not any(c[0] == 66 and c[1] == 16 and c[2] == want and c[3] == ph for c in conds):
                    raise ValueError(f"no spend sends the claim message from {ph.hex()[:12]}… (mode 16)")
                auth = ByMessage()
            requests.append(ClaimRequest(cid, ph, leaf["cumulative"], proof, auth))

        claim_sig = Signature.infinity()
        if pks:
            claim_sig = Signature.from_bytes(bytes.fromhex(str(body.get("signature", "")).removeprefix("0x")))
            if not PublicKey.aggregate_verify(pks, msgs, claim_sig):
                raise ValueError("the claim signature does not verify")
        # The bundle's own signatures: every AGG_SIG its spends require, and nothing else.
        req_pks, req_msgs = [], []
        extra = ADDITIONAL_DATA[svc.network]
        for coin_id, op, pk, msg in [(c[4], c[0], c[1], c[2]) for c in conds if c[0] in (AGG_SIG_ME, AGG_SIG_UNSAFE)]:
            req_pks.append(PublicKey.from_bytes(pk))
            req_msgs.append(msg + coin_id + extra if op == AGG_SIG_ME else msg)
        if req_pks and not PublicKey.aggregate_verify(req_pks, req_msgs, bundle_sig):
            raise ValueError("the spend bundle's signature does not verify")
        if not req_pks and spends and not bundle_sig.is_infinity():
            raise ValueError("the spend bundle carries a signature its spends do not ask for")
        for s in spends:
            state = svc.chain.reader.coin_state(s.coin.coin_id())
            if state is None or state.spent_height is not None:
                raise ValueError(f"coin {s.coin.coin_id().hex()[:12]}… is not an unspent coin on chain")
        fee = sum(s.coin.amount for s in spends) - sum(c[2] for c in conds if c[0] == 51)
        need = self.fee_per_claim * len(requests)
        if fee < need:
            raise ValueError(f"the bundle pays a fee of {fee:,} mojos; {len(requests)} claim(s) need {need:,}")
        pending = Pending(secrets.token_hex(8), requests, Signature.aggregate([claim_sig, bundle_sig]), spends, fee)
        with self.lock:
            self.queue[pending.id] = pending
        return {"request": pending.id, "status": pending.status, "claims": len(requests), "fee": fee}

    @staticmethod
    def _conditions(clvm: Clvm, spends: List[CoinSpend]) -> List[tuple]:
        """(op, a, b, c, coin id) per condition: SEND_MESSAGE as (66, mode, message, sender ph),
        CREATE_COIN as (51, ph, amount), AGG_SIG as (op, pk, message)."""
        out = []
        for s in spends:
            ran = clvm.deserialize(s.puzzle_reveal).run(clvm.deserialize(s.solution), MAX_COST, False)
            for cond in to_list(ran.value):
                items = to_list(cond) if cond.is_pair() else []
                if not items or not items[0].is_atom():
                    continue
                op = to_int(items[0])
                if op == 66 and len(items) >= 3:
                    out.append((66, to_int(items[1]), to_bytes(items[2]), s.coin.puzzle_hash, s.coin.coin_id()))
                elif op == 51 and len(items) >= 3:
                    out.append((51, to_bytes(items[1]), to_int(items[2]), None, s.coin.coin_id()))
                elif op in (AGG_SIG_ME, AGG_SIG_UNSAFE) and len(items) >= 3:
                    out.append((op, to_bytes(items[1]), to_bytes(items[2]), None, s.coin.coin_id()))
        return out

    def status_of(self, request_id: str) -> Optional[dict]:
        p = self.queue.get(request_id)
        if p is None:
            return None
        return {"request": p.id, "status": p.status, "detail": p.detail, "height": p.height,
                "claims": [{"campaign": c.campaign_id.hex(), "puzzle_hash": c.puzzle_hash.hex(), "cumulative": c.cumulative}
                           for c in p.claims]}

    # --- batching -----------------------------------------------------------------------

    def run_batch(self, wait: float = 1800, poll: float = 15) -> Optional[dict]:
        """Pay every queued request that fits one bundle. Returns what was pushed, or None."""
        svc = self.service
        with self.lock:
            queued = [p for p in self.queue.values() if p.status == "queued"]
        if not queued:
            return None
        with svc.lock:
            d = svc.d
            h = svc.chain.peak()
            take = list(queued)
            while take:
                batch = d.sequence(h, [c for p in take for c in p.claims])
                placed = set((c.campaign_id, c.puzzle_hash) for c in batch.placed)
                bad = {(r.campaign_id, r.puzzle_hash) for r, _ in batch.rejected}
                for p in take:
                    if any((c.campaign_id, c.puzzle_hash) in bad for c in p.claims):
                        p.status, p.detail = "refused", "the claim leaf refused it (already claimed, or the root moved on)"
                partial = [p for p in take if p.status == "queued" and not all((c.campaign_id, c.puzzle_hash) in placed for c in p.claims)]
                if not partial and not bad:
                    break
                take = [p for p in take if p.status == "queued" and p not in partial]  # the rest wait for the next batch
            if not take:
                return None
            spends = list(batch.spends) + [s for p in take for s in p.spends]
            signature = Signature.aggregate([p.signature for p in take])
            last = batch.generations[-1]
            successor = Coin(last.spends[0].coin.coin_id(), d.full_puzzle(last.new_state).tree_hash(), 1)
            try:
                svc.chain.push(spends, signature)
                at = svc._wait(successor.coin_id(), wait, poll)
            except (PushRefused, RuntimeError) as e:
                for p in take:
                    p.detail = f"push failed, will retry: {e}"
                svc.boot()  # the distributor may have moved under us; rebuild from chain
                return {"pushed": False, "error": str(e)}
            d.apply_batch(batch)
            for p in take:
                p.status, p.height, p.detail = "confirmed", at, None
            return {"pushed": True, "requests": len(take), "claims": len(batch.placed),
                    "generations": len(batch.generations), "height": at}
