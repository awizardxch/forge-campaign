# FLPCR — Getting to testnet11

> For running this repo on a local machine. Everything in this repo runs on the in-process
> simulator, and `scripts/deploy_testnet.py` runs it on testnet11. This page covers what works now,
> the distributors deployed so far and what each proved (§5), the script's commands and guards (§6),
> and what is left.

## 1. Local setup (works today)

```bash
git clone https://github.com/awizardxch/forge-campaign.git
cd forge-campaign
git checkout claude/practical-keller-xjj6zd     # until it is merged

# Rue compiler (Rust toolchain needed); 0.8.4 is Forge's version
cargo install rue-cli --version 0.8.4 --locked

python -m venv .venv
.venv/bin/pip install chia-wallet-sdk==0.36.0 pytest clvm   # Windows: .venv\Scripts\pip

python contracts/build.py          # compiles, checks every pin
.venv/Scripts/python.exe -m pytest tests -q   # 378 tests (.venv/bin/python elsewhere)
python scripts/mutate.py           # optional, ~3 min: every assertion is load-bearing
```

The engine alone (`flpcr/` minus `flpcr/chain/`) needs only the standard library.

## 2. What testnet needs that the simulator does not

| Piece | Simulator today | Testnet11 |
|---|---|---|
| Dev fee address and admin | test puzzles (`dev`, `admin`) | The Speechless dev multisig, curried as both `dev_fee_ph` (receives the 3% fee and any forfeited dispute bond) and `admin_ph` (may reclaim for a creator, cancel a rotation and resolve a dispute) at launch, fixed for the distributor's life. Testnet: `txch1zm9z9h4fjn88qa7f82a9n5lga2qyydd8l53jqw9qm2z5mh0ge6kqwt5jps`. Mainnet reads it from the env var `VITE_CFMM_POOL_DEV_FEE_ADDRESS_MAINNET`; never hard-code it |
| Router, operator, guardian keys | anyone-can-spend test puzzles (`Keys` in `flpcr/chain/sim.py`) | Router and operator: a standard wallet key each, signing the `SEND_MESSAGE` coin (Sage or a script). Guardian: a Forge multisig safe (`forgeMultisig.md`). `scripts/deploy_testnet.py` stands every role in with one Sage wallet's derivations: router 1, operator 2, guardian 3, creator 4, holder 5, delegate 6, rotation target 7, **disputer 8** |
| Dispute bonds | `SimWorld.dispute` funds a `disputer` test coin; `xch_balance` reads it back | `Config.testnet` sets `dispute_bond` to 0.1 TXCH (100,000,000,000 mojos; production 5 XCH). The disputer's own coin spend creates the bond launcher with exactly that amount of bare XCH, whatever the reward asset, plus change; the bond coin sits at the distributor's nonce-3 `p2_delegated_by_singleton` until `resolve` pays it to the disputer (upheld) or the dev multisig (rejected) |
| Creator and holder authorizations | coins at labeled test puzzles, or BLS wallets (`sim.bls`) | Create: a spend of a coin at the creator's own address that sends the `"k"` message (a signature is refused). Claim: the holder's key signs the leaf (`AGG_SIG_UNSAFE`, no coin at the address) or a coin at the address sends `"k" + leaf` (mode 16); neither names a generation. `deploy_testnet.py claim --by-key` claims by key signature, and `claim-all [--campaign]` claims for every address of the Sage wallet with a leaf in the active root: one Sage signing request (`Wallet.sign_claims`, the stand-in spend), one fee coin spend, one bundle of chained generations. The testnet fixture gives LP to derivations 10-14 too, so one wallet holds several addresses. Reclaims and delegate grants also accept a signature |
| Pushing spends | `Simulator.spend_coins` | A full node RPC `push_tx`, or coinset.org. The driver returns plain `CoinSpend`s plus, in `Prepared`, the messages, signatures, network fee and `creations` (coins the caller's own spend must create: a dispute's bond launcher) the bundle must carry. Only signing and pushing are new |
| Chain data for the engine | `FixtureSource` | **Built for LP holders:** `LiveLpSource` (`flpcr/live_source.py`) crawls every holder of each V16 pool's LP CAT (`flpcr/lp_crawl.py`, from the pool's LP mint-eve puzzle hash forward, custody excluded, cached in the gitignored `lp-crawl-<network>.json`), with the LP asset ids from the Forge responder's `/api/pools`. Checked on all 8 V16 testnet pools: crawled holdings plus the 1-LP burned floor equal `total_lp` exactly (first crawl 16 s, updates under 1 s). Still to build: fee events from `forge_resync` (dispatches by protocol: V16 = 17, V15 = 16, V14 = 15), so live campaigns are time-share only |
| NFT holdings for `NFT_HOLD` campaigns, gates and boosts | `ChainNftSource` reads the simulator (`flpcr/chain/nfts.py`) | **Built:** `NodeReader` (`flpcr/chain/node_reader.py`) answers the same three calls, `coin_state`, `children`, `coin_spend`, from a full node or coinset.org, with caching. `ChainNftSource(NodeReader, collections=…)` reads a DID's NFTs, or a `col1` collection by hash-checked metadata (`ipfs://` through public gateways) |
| Address links | verified in tests with generated keys | Sage's `signMessageByAddress`. Check one real Sage signature against `flpcr.links.verify` first |
| Finding the distributor again | the driver holds it in memory | Persist the launcher id plus the driver's state (state value, claim slot records, delegation slot records, lineage). Or rebuild it by replaying the singleton's spends, the way `forge_v16_resync` rebuilds a pool |
| Fees | none, except `delegate_fee` where a test sets it | The network fee comes from the creator's, holder's or operator's own coin spend. Every delegate grant or revoke must leave `delegate_fee` (testnet 1,000,000 mojos). An XCH bundle tolerates extra XCH (it goes to the fee); a CAT ring does not |
| Clocks | `Config.testnet`: 8-block disputes, 16-block minimum campaign, 56-block minimum claim window, 24-block rotation delay | Same numbers work on testnet11 (~2.5 min disputes, ~5 min campaigns); production 38 / 77 / 146 / 4,608 |

## 3. Suggested order

1. **Deploy script** (`scripts/deploy_testnet.py`): launch one XCH distributor and one for a registry CAT
   with real keys and one `Config.testnet` (dev multisig as `dev_fee_ph` and `admin_ph`), run `init`,
   and save the launcher ids and driver state to a local JSON file (never committed). Build it on the
   driver's `LeafCall` / `prepare` / `apply` flow, not on `sim.py`. The script writes
   `state-<network>.json` by default; `FLPCR_STATE` names another state file, so two distributors (or a
   second operator) keep separate records. `FLPCR_NODE_URL` and `FLPCR_FEE` set the node and the
   per-bundle network fee.
2. **One manual campaign.** Create through a spend of the creator's own coin (supplying `amount` and
   sending `"k" + campaign_id`) plus the router's message, with `start` at least 32 blocks ahead; post a
   hand-built root; claim by a spend from the holder's address and by key signature (`claim --by-key`,
  `claim-all`); reclaim after the window (at least 56
   blocks on testnet). Then an admin reclaim, a delegate grant (paying `delegate_fee`) and a rotation
   proposal and cancel. This is `TestLifecycle` and its neighbours, run on testnet.
3. **A dispute, both ways** (`TestDisputes` on testnet). Fund the `disputer` role (`fund-roles`), then
   in this order:
   - `post` a root, then `dispute` inside its window: the disputer's coin creates the bond launcher
     (0.1 TXCH plus change), the bundle carries the launcher's spend and the `dispute` action, and
     the state file records it (`status` prints the rotation but not yet the dispute; read
     `ChainState.dispute`); the root does not activate when `activates_at` passes.
   - `resolve --upheld 1` (the guardian, derivation 3): the root is revoked, the bond returns to the
     disputer's address (check its balance), and `post` reposts under the same id.
   - `post` again, `dispute` again, then `resolve --upheld 0`: the root stays pending with a restarted
     window, the bond lands at the dev multisig, and the root activates on the next spend after the
     new `activates_at`. The script's `resolve --by` accepts the guardian only; an admin resolve is a
     spend from the dev multisig itself.
   - Meanwhile check that `veto` and `post` are refused while the dispute is open, and that a `claim`
     against the previous active root still pays.
4. **Live Source** for the engine against every live Forge pool (V16, plus the V15 and V14 pools kept live), listed from the registries; check that two replays from scratch produce
   identical roots. *Partly built:* LP holders of every V16 pool (`post --source live`, §6); fee
   events and the V15/V14 pools are not.
5. **Operator bot.** Post a root whenever the previous one is active, and post the root covering a
   campaign's `end` within a few blocks of it. It must notice an open dispute (`ChainState.dispute`):
   posting is refused until it is resolved.
6. **Watcher.** Replay each pending root and alert or veto on a mismatch; anyone else's watcher disputes
   instead, and the guardian's resolves.
7. **API and the forge-ui tab** (`API.md`, SPEC §9).

## 4. Things to keep in mind

- **Slack.** Conservation is bundle-wide. A bundle with spare XCH will quietly fund a short XCH deposit
  from that spare. That is safe (the reserve is always whole) but it can surprise whoever paid the fee.
- **Batches that collide.** `Distributor.prepare` refuses, with `BundleCollision`, a batch whose actions
  cannot share one singleton spend: two actions touching the same claim or delegation slot, including a
  shared neighbour (two first claims between the same two slots, two claims through one delegate), or
  two reserve payments of the same amount to the same puzzle hash. It does not split them for you;
  send them in separate spends. For claims, `Distributor.sequence` does the splitting: it chains
  generations of the singleton in one bundle under a cost budget (`apply_batch` after confirmation).
- **Unresolved slots.** A non-empty `Prepared.unresolved` means the local slot records are behind the
  chain. Resync before pushing; consensus would refuse the bundle.
- **Every message but a claim's is per singleton coin.** The router's, the creator's, a delegate
  grant's, the admin's: a bundle built against a stale singleton coin fails, because its messages name a
  coin that is already spent. Rebuild against the current coin; a user's signed spend has to be signed
  again. A claim's signature or message names no generation, so it survives a stale coin and lands in
  whichever generation the sequencer puts it.
  Grants and revokes cost `delegate_fee` so that spending the singleton through `delegate` just to
  stale other people's bundles is not free.
- **A dispute's bond is XCH, outside the reserve.** Even on a CAT distributor the bond launcher and the
  bond coin are bare XCH; they never join the reserve's CAT ring, and `Distributor.resolve` spends the
  bond coin beside the singleton. A dispute bundle needs the disputer's coin to create exactly
  `dispute_bond` at `bond_launcher_hash` (`Prepared.creations`); any other amount is refused on chain.
- **Never run two scripts against one distributor state file.** Both spend the singleton and both write
  the record, so one of them builds against a spent coin or overwrites the other's slot records. Run
  one command at a time per `FLPCR_STATE`.

## 5. Distributors on testnet11

Each distributor is a separate launch with its own state file (`FLPCR_STATE`); a leaf change means a
new distributor, because an old one keeps the puzzles it was launched with.

| Distributor | State file | What it proved |
|---|---|---|
| `25b2b9f2…` (first) | `state-testnet11.json` | The first campaigns end to end |
| `3e2338da…` (second) | `state-testnet11-v2.json` | Disputes |
| `38dd21c6…` (third) | `state-testnet11-v3.json` | Generation-free claims. Spent after the leaves changed by rebuilding its exact leaves from source into a separate folder and pointing `FLPCR_COMPILED` at it: a finished campaign was claimed through a live-source final root, and the last 4 mojos were reclaimed |
| `b1df2ca9…` (fourth, 2026-10-20) | `state-testnet11-v4.json` | **Excluded addresses.** 1,000 real A2 LP was moved to derivation 11, which the campaign then excluded (`--exclude`). Both roots paid only derivation 0: 0.028292 TXCH, then the whole 0.097 TXCH budget. The claim leaf refused derivation 11 with the list revealed and with it hidden, while derivation 0's claim built |
| `62b6d188…` (fifth, 2026-10-20) | `state-testnet11-v5.json` | **NFT campaigns.** DID campaign `baa243a2` paid in exact proportion to NFT count: 2:1 between derivations 12 and 13, and derivations 14 and 15 only from their mint block. Collection campaign `aeb401ef` on `col1ch4n5ugp…` (the aWizard DID + collection id `ae43c809-…`): three NFTs minted through Sage `bulk_mint_nfts`, reusing metadata Studio Pro had pinned on MintGarden's IPFS; it paid 2:1 between derivations 14 and 15 and left out the earlier unnamed NFTs from the same DID. Both campaigns were claimed by key signature and reclaimed to the mojo; reserve 0 |

Spacescan's "Forge LP" collection page listed 1 item where the engine verified 2: Spacescan never
resolved `nft1ksu8…`'s metadata. The engine reads the file itself and checks it against the chain.

## 6. `deploy_testnet.py`: campaigns, claims and reclaims

- **`create`:**
  - `--exclude ADDRESS` (repeatable, at most 16; an address or a hex puzzle hash): the campaign never
    pays it. The list goes into the params' blacklist, and the driver records it for claims.
  - `--kind nft --collection-did did:chia:… --collection-id UUID`: an `NFT_HOLD` campaign whose target
    is the `col1` id of that DID + metadata collection id. The record stores `collection: [did_hex, uuid]`.
  - `--kind nft --target did:chia:…`: every NFT that DID minted.
- **`post --source live`:** the root is built from `Combined(LiveLpSource, ChainNftSource(NodeReader,
  collections from the records))`: every real LP holder, crawled from the chain, plus the NFT holders of
  every recorded collection. The default `--source fixture` is the testnet fixture.
- **`claim-all` sizes its fee from the bundle:** `10 × (batch.cost + 1.2M per signature + 30M)`, never
  below 0.002 TXCH (`FLPCR_FEE`). A nonzero fee under 5 mojos per unit of cost is refused
  (`INVALID_FEE_TOO_CLOSE_TO_ZERO`); a fixed 0.002 TXCH fee on a six-claim bundle (~504M cost) was.
- **`reclaim` is guarded** (`reclaim_blockers()`; `--force` overrides). It refuses while the campaign's
  emission isn't fully posted (the last root ends before the campaign does), while a root carrying the
  campaign is still pending, or while the final root has been claimable for less than `claim_window`
  blocks and holders still have unclaimed amounts in it. This is operator and front-end policy, not a
  puzzle rule: by design the puzzle returns unclaimed rewards to the creator from `end + claim_window`.
  A script once nearly reclaimed a whole unclaimed root; `tests/test_reclaim_guard.py` pins that case.

`scripts/dashboard.py` is a dev dashboard over the state files. It signs through the local Sage RPC
(no WalletConnect), so it is not the user-facing path:

- NFT campaigns are labeled "Hold an NFT from" a `col1…` collection or a DID, with a Spacescan link.
- Their APR is the yield per NFT (daily ÷ eligible NFTs, × 365 for a year) over the floor price, the
  cheapest open Dexie testnet offer of one NFT for TXCH (`api-testnet.dexie.space`, `offered=col1…`).
  With no floor it shows TXCH per NFT per day.
- The Claim all card lists each address's claimable amount now, shows a click's result in the card, and
  is disabled when nothing is due.
- Campaigns on earlier distributors are read-only and left out of claimable totals.
- Claim history includes claim-all bundles.
- Charts: gridlines at round values; roots that end before a campaign starts are skipped; the posted
  line steps up at each root's end.
