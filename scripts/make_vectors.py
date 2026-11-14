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

    cat_creates = [cat_create_vector(ring) for ring in ((7_000,), (600, 9_400))]
    launches = [launch_vector(ZERO_32), launch_vector(CAT_ASSET)]

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
        "cat_creates": cat_creates,
        "build_pin": build_pin_vector(),
        "launches": launches,
    }


CAT_ASSET = bytes.fromhex("969289f721bed5723aa03c73efd7886c204e50ff4e80a8eacfb352b74e7305a7")


def _coin_json(coin) -> dict:
    return {"parent_coin_info": coin.parent_coin_info.hex(), "puzzle_hash": coin.puzzle_hash.hex(), "amount": coin.amount}


def _spend_json(s) -> dict:
    return {"coin": _coin_json(s.coin), "puzzle_reveal": s.puzzle_reveal.hex(), "solution": s.solution.hex()}


def cat_create_vector(ring) -> dict:
    """A CAT-reward create's creator spends, built by the SDK (as tests/test_intake.py cat_bundle builds
    the bundle the intake accepts): an XCH coin sends "k" + id, returns its change and reserves the
    fee; the reward CAT's coins (a ring of `ring` amounts) leave exactly `amount`, the first returning
    the change to the creator, hinted. The site's catCreatorSpends must give the same bytes."""
    from chia_wallet_sdk import Cat, CatInfo, CatSpend, Coin, LineageProof

    from flpcr.chain.values import to_program

    clvm = Clvm()
    pk, ph = key(24)
    amount, fee = sum(ring) - 1_234, 50_000_000
    message = b"k" + hashlib.sha256(b"cat-create-campaign").digest()
    receiver = hashlib.sha256(b"cat-create-singleton").digest()
    xch = Coin(hashlib.sha256(b"cat-create-xch").digest(), ph, fee + 7_000_000)
    clvm.spend_standard_coin(xch, pk, clvm.delegated_spend([
        to_program(clvm, [66, 23, message, receiver]), to_program(clvm, [51, ph, xch.amount - fee]), to_program(clvm, [52, fee])]))
    info = CatInfo(CAT_ASSET, None, ph)
    cats, wallet = [], []
    for i, held in enumerate(ring):
        grand = hashlib.sha256(b"cat-create-grandparent" + bytes([i])).digest()
        parent = Coin(grand, info.puzzle_hash(), held)
        coin = Coin(parent.coin_id(), info.puzzle_hash(), held)
        lineage = LineageProof(grand, ph, held)
        conds = [to_program(clvm, [51, ph, sum(ring) - amount, [ph]])] if i == 0 else []
        cats.append(CatSpend(Cat(coin, lineage, info), clvm.standard_spend(pk, clvm.delegated_spend(conds))))
        wallet.append({"coin": _coin_json(coin), "lineageProof": {"parentName": grand.hex(), "innerPuzzleHash": ph.hex(), "amount": held}})
    clvm.spend_cats(cats)
    spends = list(clvm.coin_spends())
    for w, s in zip(wallet, spends[1:]):
        w["puzzle"] = s.puzzle_reveal.hex()
    return {
        "asset_id": CAT_ASSET.hex(), "amount": amount, "network_fee": fee, "message": message.hex(), "receiver": receiver.hex(),
        "xch_coins": [{"coin": _coin_json(xch), "puzzle": spends[0].puzzle_reveal.hex()}],
        "cat_coins": wallet,
        "coin_spends": [_spend_json(s) for s in spends],
    }


# The testnet11 build pin: the hosted primary distributor 7e252240's Config and operator, with this
# host's router (2f35a0ec…) and operator (bcc25714…). chia-cfmm src/lib/campaigns/pins.ts keeps the same.
TESTNET_PIN = {
    "operator_ph": "bcc2571486d48ce1c40f350ee3a8109e2b6bbaea4e48ba15e4d0068615ebb330",
    "dev_fee_ph": "16ca22dea994ce7077c93aba59d3e8ea804235a7fd232038a0da854ddde8ceac",
    "admin_ph": "16ca22dea994ce7077c93aba59d3e8ea804235a7fd232038a0da854ddde8ceac",
    "guardian_ph": "4aaecb635fc6d07a9a482137d77921fb0a3e5e4f342d70d7ba87d9736300708a",
    "router_ph": "2f35a0ec4f64a167cb2e44636bff897f81b1a4b4a0e38382c6a58360ce8ebc15",
    "fee_bps": 300, "dispute_blocks": 8, "rotate_delay": 24, "delegate_fee": 1_000_000, "min_campaign_length": 16,
    "max_campaign_length": 1_681_920, "max_campaigns": 32, "dispute_bond": 100_000_000_000,
}


def testnet_pin():
    from flpcr.build_pin import BuildPin
    from flpcr.registry import pins_fingerprint

    return BuildPin("testnet11", pins=pins_fingerprint(),
                    **{k: (bytes.fromhex(v) if isinstance(v, str) else v) for k, v in TESTNET_PIN.items()})


def build_pin_vector() -> dict:
    """The mod hashes a page needs to rebuild a distributor's puzzle hashes from its own pins."""
    from chia_wallet_sdk import Constants

    from flpcr.chain.driver import Puzzles
    from flpcr.leaves import LEAVES

    p = Puzzles(Clvm())
    pin = testnet_pin()
    return {
        "pin": {**TESTNET_PIN, "hint": pin.hint.hex()},
        "leaves": [[n, p.hash(n).hex()] for n in LEAVES],
        "action": p.hash("action").hex(), "pinned_reserve_finalizer": p.hash("pinned_reserve_finalizer").hex(),
        "p2_delegated_by_singleton": p.hash("p2_delegated_by_singleton").hex(), "slot": p.hash("slot").hex(),
        "bond_launcher": p.hash("bond_launcher").hex(),
        "singleton_top_layer": Constants.singleton_top_layer_v1_1_hash().hex(),
        "singleton_launcher": Constants.singleton_launcher_hash().hex(),
    }


def launch_vector(asset: bytes) -> dict:
    """A first-use launch's eve coin, from the pin and the creator's coins: what the page must rebuild
    before it signs a launch (flpcr.service.launch). The eve's full puzzle is what the chain reveals
    when the eve is spent, so the page's on-chain check of a later create reads the same bytes."""
    from chia_wallet_sdk import Cat, CatInfo, Coin, Constants, LineageProof

    from flpcr.chain.driver import Distributor, Puzzles
    from flpcr.service.launch import launcher_announcement

    pin = testnet_pin()
    _, creator = key(25)
    funding = Coin(hashlib.sha256(b"launch-funding" + asset).digest(), creator, 5_000_000_000)
    launcher_id = Coin(funding.coin_id(), Constants.singleton_launcher_hash(), 1).coin_id()
    p = Puzzles(Clvm())
    d = Distributor(p, pin.config_for(launcher_id, asset), pin.operator_ph)
    seed = None
    if asset != ZERO_32:
        info = CatInfo(asset, None, creator)
        grand = hashlib.sha256(b"launch-seed-grandparent").digest()
        parent = Coin(grand, info.puzzle_hash(), 9_000)
        seed = Cat(Coin(parent.coin_id(), info.puzzle_hash(), 9_000), LineageProof(grand, creator, 9_000), info)
        d.seed_reserve(seed)
    d.launcher_spend(funding)
    eve_puzzle = d.full_puzzle(d.state)
    return {
        "asset_id": asset.hex(), "creator": creator.hex(), "funding_coin": _coin_json(funding),
        "funding_coin_id": funding.coin_id().hex(), "seed_coin_id": seed.coin.coin_id().hex() if seed else None,
        "launcher_id": launcher_id.hex(), "config_hash": sha256tree(d.config_value()).hex(),
        "leaf_hashes": [d.leaf_hashes[n].hex() for n in d.leaf_hashes], "merkle_root": d.merkle_root.hex(),
        "reserve_puzzle_hash": d.reserve_ph.hex(), "reserve_full_puzzle_hash": d.reserve_full_ph.hex(),
        "reserve_parent": d.reserve_coin.parent_coin_info.hex(), "finalizer_hash": d.finalizer.tree_hash().hex(),
        "state_hash": d.state.tree_hash().hex(), "eve_puzzle_hash": d.coin.puzzle_hash.hex(),
        "eve_coin_id": d.coin.coin_id().hex(), "eve_puzzle": eve_puzzle.serialize().hex(),
        "launcher_announcement": launcher_announcement(launcher_id, d.coin.puzzle_hash).hex(),
        "slot_1st_hash": d.slot_1st.tree_hash().hex(), "delegation_1st_hash": d.delegation_1st.tree_hash().hex(),
        "bond_puzzle_hash": d.bond_ph.hex(),
        "creator_spends": launch_creator_spends(d, funding, seed, launcher_id),
    }


def launch_creator_spends(d, funding, seed, launcher_id) -> dict:
    """The creator's spends for that launch, built by the SDK as tests/test_fleet.py launch_bundle
    builds the bundle the launch intake accepts; the site's creatorSpends / catCreatorSpends with
    launch parts must give the same bytes. The funding coin sends "k" + id to the eve, creates the
    launcher (hinted), for XCH the empty reserve, asserts the launcher's announcement, returns its
    change and reserves the fee; a CAT's first coin creates the empty reserve and its change."""
    from chia_wallet_sdk import CatSpend, Constants

    from flpcr.chain.values import to_program
    from flpcr.service.launch import launcher_announcement

    clvm = Clvm()
    pk, ph = key(25)
    amount = 1_000_000 if seed is None else 7_000
    fee = 50_000_000
    message = b"k" + hashlib.sha256(b"launch-campaign" + d.cfg.reward_asset).digest()
    hint = testnet_pin().hint
    conds = [[66, 23, message, d.coin.coin_id()], [51, Constants.singleton_launcher_hash(), 1, [hint]]]
    if seed is None:
        conds.append([51, d.reserve_ph, 0])
    conds.append([61, launcher_announcement(launcher_id, d.coin.puzzle_hash)])
    deposit = amount if seed is None else 0
    conds += [[51, ph, funding.amount - 1 - deposit - fee], [52, fee]]
    clvm.spend_standard_coin(funding, pk, clvm.delegated_spend([to_program(clvm, c) for c in conds]))
    wallet = []
    if seed is not None:
        inner = [[51, d.reserve_ph, 0, [d.reserve_ph]], [51, ph, seed.coin.amount - amount, [ph]]]
        clvm.spend_cats([CatSpend(seed, clvm.standard_spend(pk, clvm.delegated_spend([to_program(clvm, c) for c in inner])))])
    spends = list(clvm.coin_spends())
    if seed is not None:
        lp = seed.lineage_proof
        wallet.append({"coin": _coin_json(seed.coin), "puzzle": spends[1].puzzle_reveal.hex(),
                       "lineageProof": {"parentName": lp.parent_parent_coin_info.hex(),
                                        "innerPuzzleHash": lp.parent_inner_puzzle_hash.hex(), "amount": lp.parent_amount}})
    return {"amount": amount, "network_fee": fee, "message": message.hex(),
            "xch_coins": [{"coin": _coin_json(funding), "puzzle": spends[0].puzzle_reveal.hex()}],
            "cat_coins": wallet, "coin_spends": [_spend_json(s) for s in spends]}


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
