#!/usr/bin/env python3
"""Write fixtures/vectors.json: what a front end must reproduce before it asks a wallet to sign.

forge-ui recomputes claim leaves, campaign ids and the claim stand-in spend in TypeScript
(docs/FORGE_SITE_TAB.md §7). These vectors are the reference it checks against; a wallet is
never asked to sign a leaf the page cannot reproduce.

    python scripts/make_vectors.py          # rewrite the file
    python scripts/make_vectors.py --check  # exit 1 if the file is stale (tests/test_vectors.py)
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chia_wallet_sdk import Clvm, SecretKey, standard_puzzle_hash  # noqa: E402

from flpcr.clvm import sha256tree  # noqa: E402
from flpcr.merkle import claim_leaf  # noqa: E402
from flpcr.model import Campaign, Kind, Params  # noqa: E402

ZERO_32 = bytes(32)  # the reward asset id for XCH
from flpcr.chain.nfts import collection_id  # noqa: E402

OUT = ROOT / "fixtures" / "vectors.json"
STAND_IN_PARENT_TAG = b"flpcr-claim-proof"  # Wallet.sign_claims in scripts/deploy_testnet.py


def key(seed: int):
    sk = SecretKey.from_seed(bytes([seed]) * 32).derive_synthetic()
    pk = sk.public_key()
    return pk, standard_puzzle_hash(pk)


def campaign_row(c: Campaign) -> dict:
    return {
        "fields": {
            "distributor_id": c.distributor_id.hex(), "creator": c.creator.hex(), "reward_asset": c.reward_asset.hex(),
            "amount": c.amount, "start": c.start, "end": c.end, "kind": int(c.kind), "target": c.target.hex(),
            "nonce": c.nonce, "fee_bps": c.fee_bps, "claim_window": c.claim_window,
        },
        "params": {
            "time_bps": c.params.time_bps, "fees_bps": c.params.fees_bps, "min_balance": c.params.min_balance,
            "blacklist": sorted(x.hex() for x in c.params.blacklist), "fee_window": c.params.fee_window,
            "boost_cap_bps": c.params.boost_cap_bps,
            "collection": [c.params.collection[0].hex(), c.params.collection[1]] if c.params.collection else None,
        },
        "params_hash": c.params.hash().hex(),
        "excluded_hash": sha256tree(sorted(c.params.blacklist)).hex(),
        "fee": c.fee, "budget": c.budget,
        "id": c.id.hex(),
    }


def vectors() -> dict:
    clvm = Clvm()
    distributor = bytes.fromhex("62b6d188" + "00" * 28)
    pk_a, ph_a = key(21)
    pk_b, ph_b = key(22)
    creator = key(23)[1]
    pool = bytes.fromhex("b0" * 32)
    did = bytes.fromhex("d1" * 32)

    lp = Campaign(distributor, creator, ZERO_32, 1_000_000_000_000, 5_000_000, 5_000_000 + 4608 * 7,
                  Kind.LP_HOLD, pool, params=Params(), claim_window=4608)
    lp_excluding = Campaign(distributor, creator, ZERO_32, 50_000_000_000, 5_000_100, 5_000_100 + 4608,
                            Kind.LP_HOLD, pool, params=Params(blacklist=frozenset({ph_b})), nonce=1, claim_window=4608)
    col = collection_id(did, "ae43c809-0000-4000-8000-000000000000")
    nft = Campaign(distributor, creator, ZERO_32, 10_000_000_000, 5_001_000, 5_001_000 + 4608 * 30, Kind.NFT_HOLD, col,
                   params=Params(collection=(did, "ae43c809-0000-4000-8000-000000000000")), claim_window=9216)
    campaigns = [lp, lp_excluding, nft]

    leaves = []
    for c, ph, cumulative in [(lp, ph_a, 1), (lp, ph_a, 123_456_789), (lp, ph_b, 987_654_321_000), (nft, ph_b, 42)]:
        leaves.append({"distributor_id": distributor.hex(), "campaign_id": c.id.hex(), "puzzle_hash": ph.hex(),
                       "cumulative": cumulative, "leaf": claim_leaf(distributor, c.id, ph, cumulative).hex()})

    # The claim stand-in (docs/FORGE_SITE_TAB.md §4.3): one zero-amount coin whose puzzle is
    # (q . ((49 pk leaf) ...)); a wallet signing it signs AGG_SIG_UNSAFE(pk, leaf) per pair.
    pairs = [(pk_a, bytes.fromhex(leaves[1]["leaf"])), (pk_b, bytes.fromhex(leaves[2]["leaf"]))]
    conditions = " ".join(f"(49 0x{pk.to_bytes().hex()} 0x{leaf.hex()})" for pk, leaf in pairs)
    puzzle = clvm.parse(f"(q . ({conditions}))")
    parent = hashlib.sha256(STAND_IN_PARENT_TAG).digest()
    stand_in = {
        "pairs": [{"public_key": pk.to_bytes().hex(), "leaf": leaf.hex()} for pk, leaf in pairs],
        "parent_coin_info": parent.hex(), "amount": 0, "solution": "80",
        "puzzle_reveal": puzzle.serialize().hex(), "puzzle_hash": puzzle.tree_hash().hex(),
    }

    return {
        "about": "FLPCR reference vectors for front-end mirrors. Regenerate with scripts/make_vectors.py.",
        "claim_tag": "flpcr-claim-v1", "campaign_tag": "flpcr-campaign-v1",
        "keys": [{"seed_byte": 21, "public_key": pk_a.to_bytes().hex(), "puzzle_hash": ph_a.hex()},
                 {"seed_byte": 22, "public_key": pk_b.to_bytes().hex(), "puzzle_hash": ph_b.hex()}],
        "campaigns": [campaign_row(c) for c in campaigns],
        "collection_id": {"did": did.hex(), "metadata_collection_id": "ae43c809-0000-4000-8000-000000000000",
                          "id": col.hex()},
        "leaves": leaves,
        "claim_stand_in": stand_in,
    }


def render() -> str:
    return json.dumps(vectors(), indent=1, sort_keys=False) + "\n"


def main(argv) -> int:
    text = render()
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run python scripts/make_vectors.py")
            return 1
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
