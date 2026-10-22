# FLPCR (forge-campaign): notes for agents

Forge Liquidity Pool Campaign Rewards: Merkl-style incentive campaigns for Forge LP and Chia CATs.
Start with `README.md`, then `docs/SPEC.md`. `docs/TESTNET.md` is the plan for testnet11;
`docs/AGENT_TOOLS.md` is the contract for every `flpcr.*` tool (who signs what). The aWizard agent's skill
for this project is `docs/skills/forgeCampaigns.md` in `awizardxch/aWizard-Familiar`.

## Open review

`reviews/2026-10-19/FINDINGS.md` is the pre-testnet review of `dea1d7d`. Read it before changing a leaf or
launching anything. Its probes are in `reviews/2026-10-19/probes/` (run from the repo root with
`PYTHONPATH=.:reviews/2026-10-19/probes/<lane>`); they were written against the old pins and need a
rerun against the new ones.

- **Applied on `fix/review-2026-10-19-onchain`, in one re-pin of every leaf:** FLPCR-02 (delegation
  slot list), -03 (dispute window from `h + 32`), -04 (admin reclaim), -05 (height guards pinned), -06
  (delayed, cancellable rotation), -07 (creator's coin spend, 32-byte checks), -08 (claim window floor),
  -12 (delegate fee), -13 (`BundleCollision`), -14 (one slot per delegation key), -15 (start lead and
  horizon), -16 (refusal pinning), and the driver restructure G-1 to G-4 and the CAT twins (G-10).
- **Applied off chain on the same branch:** FLPCR-01 (time-averaged fee share over `params.fee_window`),
  -09 (fee budget in fixed campaign-relative buckets, so root cuts move nothing), -10 (NFT collection
  reader), -11 (links v2: signed sequence, either side unlinks, log committed in the manifest), -17
  (exact fee weights), the blacklisted-wrapper forwarding note, G-5, G-6, G-9; and the router's
  `RouterRules` (minimum amount and per-day rate per asset, live campaigns per creator). The values of
  `RouterRules` are the owner's to set.
- **Completion gate (2026-10-19):** 327 tests pass (with the bonded dispute: `tests/test_chain.py::TestDisputes`
  and `tests/test_distributor.py::test_dispute_and_resolve`); `scripts/mutate.py` on the ten-leaf build (2026-10-20) ends "82 killed, 0
  unargued survivors, 9 did not build". The guard-line mutation also found that no test proved a changed slot is
  *spent*, not just recreated (a stale claim or delegation slot would have paid twice or outlived a
  revoke); `TestSpentSlotsStaySpent` now does.
- **Generation-free claims (2026-10-19):** 345 tests pass (new: `TestOnlyTheHolderClaims`,
  `TestDelegatedClaims.test_a_delegate_claims_by_key_signature`, `TestSequencer`, each twice); the
  mutation rerun on `claim` and `shared` ends "22 killed, 0 unargued survivors, 3 did not build"
  (`authorize_claim(` added to `GUARDS`). No full rerun since.
- **Excluded addresses and NFT campaigns (2026-10-20):** 378 tests pass (new: `TestExcludedAddresses`,
  `TestNftCampaigns`, `tests/test_nfts.py::TestCollectionByMetadata`, `tests/test_lp_crawl.py`,
  `tests/test_reclaim_guard.py`). Mutation on `create`, `claim` and `shared` after the exclusions: "44
  killed, 0 unargued survivors, 3 did not build"; on `create` after `NFT_HOLD`: "18 killed, 0 unargued
  survivors". Proven on testnet11 by the fourth (`b1df2ca9…`) and fifth (`62b6d188…`) distributors
  (`docs/TESTNET.md` §5).

## Owner rules not yet built (2026-10-19)

- The front end sets the total amount and the campaign length; emission per day is total / days, equal every day.
- Holder shares come from past holdings, time-averaged, never the live block (as Merkl roots do): a position held for one or two blocks must average to about zero. Confirm the engine's fee lookback excludes the current block and set a floor on `params.fee_window`.
- A wallet with several addresses claims for all of them in one bundle. Built differently from the rule as written (one coin spend per address): the wallet signs every address's leaf in one approval (key signature, no coin at the addresses) and one coin spend pays the fee; `Distributor.sequence` chains generations for real collisions. Confirmed by the owner (2026-10-20), with FLPCR-07 below.

## Build and test

```bash
cargo install rue-cli --version 0.8.4 --locked       # exactly 0.8.4: reproduces upstream's pinned hashes
pip install chia-wallet-sdk==0.36.0 pytest clvm
python contracts/build.py --check   # compiled/ matches the sources and every pin; run before trusting anything
.venv/Scripts/python.exe -m pytest tests -q   # 378 tests, all on the in-process simulator
python scripts/mutate.py            # optional, ~3 min; must end "0 unargued survivors"
```

`flpcr/leaves.py` is the one leaf list (ten leaves; its order fixes every puzzle hash) plus `EXTRA`, the
bond launcher, a plain coin puzzle compiled and pinned beside them; `build.py`, `mutate.py` and the
driver all read it. `build.py` also enforces the upstream source sha256 pins.

## Rules the code depends on

- **Upstream puzzles are never edited.** `contracts/puzzles/upstream/` is slot-machine@2d37ba1 verbatim;
  `build.py` fails if a hash moves. Only `contracts/puzzles/flpcr/` is ours.
- **A leaf change is a reviewed pin change.** After editing a leaf, run `python contracts/build.py --repin`
  and commit `pins.json` and `compiled/` with it. Any change to `Config` or `State` in `shared.rue` moves
  every leaf hash and changes every distributor's puzzle hash, so already-launched distributors keep the
  old puzzles.
- **`flpcr/distributor.py` mirrors the puzzles.** Change both together; `tests/test_differential.py` holds
  them to the same payouts and refusals. Its `Config` is the only config class; the driver curries the
  leaves from it.
- **A refusal test must be refused for its own reason.** `refused()` accepts only `clvm raise` or a
  `Simulator error`. Every puzzle refusal is the same `clvm raise`, so which assertion fired is pinned
  only by the mutation run; consensus refusals name their rule, and tests pin those names with
  `expect=`. A probe that builds a claim by hand must still authorize it, or it passes for the wrong
  reason.
- **Build spends through the driver.** A per-leaf builder on `Distributor` returns a `LeafCall`;
  `prepare(calls)` returns the spends (a call's `extra_spends` included: the bond launcher, the bond
  coin) plus the messages, signatures, fee and `creations` (coins the caller's own spend must create)
  the bundle must carry; `apply(prepared)` after it confirms. `BundleCollision` means split the batch
  across spends.
- **`Clvm.alloc([83, 5])` encodes a byte string, not a list.** Build values with `flpcr/chain/values.py`.
- **Conservation is bundle-wide.** Spare XCH anywhere in a bundle can cover a short XCH deposit.

## Decisions already made (don't relitigate)

- 3% dev fee, paid in the create spend to `dev_fee_ph` (the Speechless dev multisig); no fee collection.
- Admin: `admin_ph` is the dev multisig (normally the same address as `dev_fee_ph`). It may reclaim for
  a creator, cancel a pending rotation and resolve a dispute; it can never take funds.
- Disputes are open to anyone with a bond of `dispute_bond` (XCH whatever the reward asset: production
  5 XCH, testnet 0.1 TXCH), funded by the disputer's own coin spend through the bond launcher; the
  guardian or admin resolves; upheld bonds are refunded to the disputer, rejected bonds go to the dev
  multisig and the window restarts. The bond coin (nonce 3) never enters the reserve, and `veto` is
  refused while a dispute is open.
- Claims: cumulative, at any interval. Only the holder, or a delegate the holder granted, can claim
  (`authorize_claim` in `shared.rue`): `AUTH_SIGNATURE` (the address's synthetic key, `AGG_SIG_UNSAFE`
  of the leaf; no coin needed), `AUTH_MESSAGE` (a coin at the address sends `"k" + leaf`, mode 16), or
  `AUTH_DELEGATE` whose delegate authorizes either way. No claim names a generation, so
  `Distributor.sequence` packs many into one bundle; replay pays nothing (slot `claimed`, `amount > 0`)
  and the payout always goes to the leaf's puzzle hash. **Confirmed by the owner (2026-10-20):** FLPCR-07
  for claims ("to claim a person has to spend a mojo") is no longer puzzle-enforced; the wallet's one
  fee-paying coin spend in the claim bundle plays that role off-puzzle (the claimant still pays for an
  on-chain transaction). Every other action keeps mode 23
  / `AGG_SIG_ME`.
- Reclaim: by the creator, the creator's delegate, or the admin, from `end + claim_window`; always paid
  to the creator. Holders can claim until the reclaim lands.
- Creation needs our router's message **and** the creator's own coin spend sending `"k" + id` (a
  signature is refused).
- Operator rotation: the guardian proposes, it takes effect `rotate_delay` later (production 4,608 ≈ one
  day, testnet 24), the old operator posts until then, and the admin can cancel it.
- Delegate fee: every grant or revoke leaves `delegate_fee` as network fee (production 50,000,000 mojos,
  testnet 1,000,000).
- Campaign length: testnet from 16 blocks, production from 77, max 365 days; `start` from `h + 32` to
  `h + max_campaign_length`. The claim window is the creator's choice (floor: `3 × dispute_blocks + 32`,
  testnet 56, production 146).
- Reward assets: XCH, or a CAT in a Forge registry pool (router rule).
- NFT collections are identified by minter DID, or by a `col1` id = sha256(DID hex + metadata
  `collection.id`), which counts an NFT only if the DID minted it **and** its hash-checked metadata
  names the collection. Gates and boosts live in the campaign params.
- **Excluded addresses (2026-10-20):** the burn address `0x…dead` never earns, and a creator may exclude
  up to 16 addresses (`params.blacklist`, stored as `Campaign.excluded`, its tree hash). The engine
  gives them no share and the claim leaf refuses them.
- **NFT campaigns (2026-10-20):** `Kind.NFT_HOLD = 3`, one share per NFT, time-weighted, nothing staked,
  no fee weighting.
- **Every holder in every root (2026-10-20):** each root lists every eligible holder whether or not they
  claim; what nobody has claimed when the window ends goes back to the creator.
- **The open pool (balance/365 per day) is a separate puzzle, for later** (`docs/OPEN_POOLS_DESIGN.md`).
  LP and NFT Merkl campaigns on this distributor come first.
- **Reclaim guard:** our tools refuse a reclaim while holders could not yet claim the final root
  (`reclaim_blockers()`, `--force` overrides). Operator policy, not a puzzle rule.
- Puzzles live in this repo, not `forge-puzzles`.

## Testnet11: where the work starts

Five distributors have run on testnet11 (`docs/TESTNET.md` §5); `state-testnet11*.json` are their
records (never committed). **Never run two scripts against one distributor state file:** both spend
the singleton and both write the record. To spend an older distributor after a leaf change, rebuild its
exact leaves into a separate folder and set `FLPCR_COMPILED`. The original plan, in order (`docs/TESTNET.md` §3):

1. **Real keys.** Replace the `Keys` stand-ins (`flpcr/chain/sim.py`) for router, operator and guardian,
   and set the dev multisig as both `dev_fee_ph` and `admin_ph` (testnet
   `txch1zm9z9h4fjn88qa7f82a9n5lga2qyydd8l53jqw9qm2z5mh0ge6kqwt5jps`; mainnet reads
   `VITE_CFMM_POOL_DEV_FEE_ADDRESS_MAINNET`, never hard-coded). The driver returns plain `CoinSpend`s
   plus what the bundle must carry, so the new parts are signing and pushing (full node `push_tx` or
   coinset.org).
2. **`scripts/deploy_testnet.py`.** Launch an XCH distributor and a CAT one, run `init`, and save the
   launcher ids and driver state (state value, claim and delegation slot records, lineage) to an
   uncommitted JSON file. Use the driver's `LeafCall` / `prepare` / `apply`, not `sim.py`.
3. **One manual campaign end to end:** create (creator's coin spend plus router message), post a root,
   claim, reclaim. This is `tests/test_chain.py::TestLifecycle` against testnet. Then a dispute both
   ways (`dispute`, `resolve --upheld 1`, repost, `dispute`, `resolve --upheld 0`): `TestDisputes`. The
   script's `disputer` role is derivation 8, and `FLPCR_STATE` names its state file.
4. **Check Sage signatures** against `flpcr.links.verify` (address links), the reclaim and delegate
   `AGG_SIG_ME`, the claim `AGG_SIG_UNSAFE` stand-in (`claim-all`), and the coin spends that carry
   create and claim messages, before relying on them.
5. **Live data:** built for LP holders and NFTs. `flpcr/lp_crawl.py` crawls every V16 pool's LP holders
   (custody excluded, cached in the gitignored `lp-crawl-<network>.json`), `flpcr/live_source.py`
   `LiveLpSource` serves them (time share only), and `flpcr/chain/node_reader.py` `NodeReader` is the
   node adapter for `nfts.py`. `deploy_testnet.py post --source live` combines them. Still open:
   `forge_resync` fee events.
