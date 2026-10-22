"""Sign spends with a secret key held by this process, the way a full node checks them.

The hosted operator cannot ask a wallet to sign: it holds its own key. `KeySigner`
runs each spend it is asked to sign, collects the AGG_SIG conditions naming one
of its public keys, and signs exactly what consensus verifies:

    AGG_SIG_UNSAFE (49)  the message alone
    AGG_SIG_ME     (50)  message + coin id + the network's AGG_SIG_ME additional data

The additional data is the network's genesis challenge (`ADDITIONAL_DATA`).
Conditions for keys it does not hold are left for their owners: a claim bundle
aggregates the claimants' own signatures with the operator's.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence

from chia_wallet_sdk import Clvm, CoinSpend, SecretKey, Signature

from .values import to_bytes, to_int, to_list

AGG_SIG_UNSAFE = 49
AGG_SIG_ME = 50
MAX_COST = 11_000_000_000

# AGG_SIG_ME additional data: each network's genesis challenge.
ADDITIONAL_DATA = {
    "mainnet": bytes.fromhex("ccd5bb71183532bff220ba46c268991a3ff07eb358e8255a65c30a2dce0e5fbb"),
    "testnet11": bytes.fromhex("37a90eb5185a9c4439a91ddc98bbadce7b4feba060d50116a067de66bf236615"),
}


class KeySigner:
    def __init__(self, keys: Iterable[SecretKey], additional_data: bytes):
        self.keys: Dict[bytes, SecretKey] = {sk.public_key().to_bytes(): sk for sk in keys}
        self.additional_data = additional_data

    @classmethod
    def for_network(cls, keys: Iterable[SecretKey], network: str) -> "KeySigner":
        if network not in ADDITIONAL_DATA:
            raise ValueError(f"unknown network {network!r}")
        return cls(keys, ADDITIONAL_DATA[network])

    def required(self, spends: Sequence[CoinSpend]) -> List[tuple]:
        """(public key, message to sign) for every AGG_SIG condition this signer can answer."""
        clvm = Clvm()
        out = []
        for spend in spends:
            ran = clvm.deserialize(spend.puzzle_reveal).run(clvm.deserialize(spend.solution), MAX_COST, False)
            for cond in to_list(ran.value):
                items = to_list(cond) if cond.is_pair() else []
                if len(items) < 3 or not items[0].is_atom():
                    continue
                op = to_int(items[0])
                if op not in (AGG_SIG_UNSAFE, AGG_SIG_ME):
                    continue
                pk, message = to_bytes(items[1]), to_bytes(items[2])
                if pk not in self.keys:
                    continue
                if op == AGG_SIG_ME:
                    message = message + spend.coin.coin_id() + self.additional_data
                out.append((pk, message))
        return out

    def sign(self, spends: Sequence[CoinSpend]) -> Signature:
        sigs = [self.keys[pk].sign(message) for pk, message in self.required(spends)]
        return Signature.aggregate(sigs) if sigs else Signature.infinity()
