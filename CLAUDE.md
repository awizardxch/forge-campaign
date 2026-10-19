# FLPCR (forge-campaign): notes for agents

Forge Liquidity Pool Campaign Rewards: Merkl-style incentive campaigns for Forge LP and Chia CATs.
Start with `README.md`, then `docs/SPEC.md`. `docs/TESTNET.md` is the plan for testnet11.

## Build and test

```bash
cargo install rue-cli --version 0.8.4 --locked       # exactly 0.8.4: reproduces upstream's pinned hashes
pip install chia-wallet-sdk==0.36.0 pytest clvm
python contracts/build.py --check   # compiled/ matches the sources and every pin; run before trusting anything
python -m pytest -q                 # 166 tests, all on the in-process simulator
python scripts/mutate.py            # optional, ~3 min; must end "0 unargued survivors"
```

## Rules the code depends on

- **Upstream puzzles are never edited.** `contracts/puzzles/upstream/` is slot-machine@2d37ba1 verbatim;
  `build.py` fails if a hash moves. Only `contracts/puzzles/flpcr/` is ours.
- **A leaf change is a reviewed pin change.** After editing a leaf, run `python contracts/build.py --repin`
  and commit `pins.json` and `compiled/` with it. Any change to `Config` or `State` in `shared.rue` moves
  every leaf hash and changes every distributor's puzzle hash, so already-launched distributors keep the
  old puzzles.
- **`flpcr/distributor.py` mirrors the puzzles.** Change both together; `tests/test_differential.py` holds
  them to the same payouts and refusals.
- **A refusal test must be refused for its own reason.** `refused()` accepts only `clvm raise` or a
  `Simulator error`. A probe that builds a claim by hand must still authorize it, or it passes for the
  wrong reason.
- **`Clvm.alloc([83, 5])` encodes a byte string, not a list.** Build values with `flpcr/chain/values.py`.
- **Conservation is bundle-wide.** Spare XCH anywhere in a bundle can cover a short XCH deposit.

## Decisions already made (don't relitigate)

- 3% dev fee, paid in the create spend to `dev_fee_ph` (the Speechless dev multisig); no fee collection.
- Claims: cumulative, at any interval. Only the holder, or a delegate the holder granted, can claim.
- Reclaim: creator-only (or the creator's delegate), from `end + claim_window`. Holders can claim until
  the reclaim lands.
- Campaign length: testnet from 16 blocks, production from 77, max 365 days. The claim window is the
  creator's choice (floor: 2 × `dispute_blocks`).
- Reward assets: XCH, or a CAT in a Forge registry pool (router rule). Creation needs our router's message.
- NFT collections are identified by minter DID. Gates and boosts live in the campaign params.
- Puzzles live in this repo, not `forge-puzzles`.

## Testnet11: where the work starts

Nothing here has touched a real chain yet. In order (`docs/TESTNET.md` §3):

1. **Real keys.** Replace the `Keys` stand-ins for router, operator and guardian, and set the dev multisig
   address as `dev_fee_ph`. The driver returns plain `CoinSpend`s, so the new parts are signing and
   pushing (full node `push_tx` or coinset.org).
2. **`scripts/deploy_testnet.py`.** Launch an XCH distributor and a CAT one, run `init`, and save the
   launcher ids and driver state (state value, slot and delegation records, lineage) to an uncommitted JSON
   file.
3. **One manual campaign end to end:** create, post a root, claim, reclaim. This is
   `tests/test_chain.py::TestLifecycle` against testnet.
4. **Check Sage signatures** against `flpcr.links.verify` (address links), the claim and reclaim
   `AGG_SIG_ME`, and delegate grants, before relying on them.
5. **Live data:** a node adapter for `flpcr/chain/nfts.py` (three calls) and a live engine `Source`.
