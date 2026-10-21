# FLPCR — Forge Liquidity Pool Campaign Rewards — Specification

> Incentive campaigns for Forge liquidity and Chia CATs. Anyone can fund a reward budget on a
> Forge pool or a CAT over a block range. Holders earn from it every block and claim whenever
> they like. Once the claim window the creator chose has passed, the creator may take back
> everything holders left unclaimed; until they actually do, holders can still claim.
> **FLPCR is the working name.** It appears in identifiers (`flpcr`, `flpcr-*-v1` tags), so a
> rename is a find-and-replace until something is deployed. Merkl is the reference design, not a
> dependency; nothing here uses Merkl's name, contracts or API.
> Status: **v0.4, 2026-10-19.** Engine, reference model **and the distributor puzzles** are built,
> with the on-chain fixes from the pre-testnet review (`reviews/2026-10-19/FINDINGS.md`) applied in one
> re-pin. The puzzles run on the in-process simulator (chia-wallet-sdk 0.36.0) for XCH and CAT rewards,
> agree with the reference model on randomized scenarios, and every leaf assertion and emitted guard is
> pinned by a test or argued (§12). On testnet11 since 2026-10-20 ([`TESTNET.md`](TESTNET.md)): LP
> campaigns on live holders, NFT campaigns, excluded addresses. Not externally audited.

**Decisions so far**

| | |
|---|---|
| Dev fee | **3%**, taken out of the deposit, as Merkl does: a 1,000 deposit funds a 970 budget. It is **paid at creation**, in the create spend itself, straight to the dev fee address (the Speechless dev multisig). Nothing accumulates in the distributor and there is nothing to collect |
| Claims | **Pull, cumulative, at any time**, as Merkl does. Each root lists what a holder has earned so far; a claim pays the difference from what they already claimed. **Only the holder can claim**, by their address's key signing the claim or a coin at their address sending it, **or a delegate the holder granted**, the same way at the delegate's address (a hot wallet claiming for a cold wallet; rewards still go to the cold wallet). No authorization names a generation of the distributor, so a sequencer packs many holders' claims into one bundle (§5.4) |
| Claim window | **Set by the creator at creation** and committed in the campaign id, so the terms are complete up front. It opens reclaiming; it does not close claiming |
| Leftovers | From `end + claim_window`, the **creator may reclaim** everything not claimed: budget nobody earned plus earnings nobody claimed. The creator's delegate, or the **admin** (the dev multisig), may submit the reclaim instead; **the payment always goes to the creator**. **Holders can claim until the reclaim lands**: a 24 h window with no reclaim after 48 h still pays a late claimer |
| Campaign length | Per distributor. **Testnet: from ~5 min** (16 blocks, 8-block disputes). **Production: likely from 24 min** (77 blocks, 38-block ≈ 12 min disputes). Maximum **365 days**. Any of these can change for a future distributor |
| Claim frequency | **Any interval within the campaign**, at the holder's discretion: every root, once at the end, or anything between. Each claim bundle costs a network fee, paid by one coin spend of the claimer's wallet, so how often is their call. A new root becomes claimable at most every `dispute_blocks + 32` blocks |
| Assets | **Any CAT that trades in a Forge registry pool**, plus XCH, for the reward asset and for a `CAT_HOLD` target. The pool's oracle gives every such asset an on-chain price. An `LP_HOLD` target must be a registered pool |
| Gates and boosts | A campaign can **require** holdings to earn at all (at least N NFTs of a collection, e.g. the Wizards, or at least X of a CAT) and **boost** holders who meet further requirements (+N% weight per tier, up to a cap the creator sets). Engine-side rules, committed in the campaign id; no puzzle change (§6.8) |
| Who runs it | The puzzle is on chain and permissionless to read. **Creation needs the distributor's router key and the creator's own coin spend**: our router now, so we only score and monitor campaigns our router created. Later, anyone can launch their own distributor with their own router, engine and front end |
| Admin | The **dev multisig** (`admin_ph`, normally the same address as `dev_fee_ph`). It may reclaim a campaign for its creator once the claim window has passed (paid to the creator), cancel a pending operator rotation, and resolve a dispute. It cannot move funds anywhere else |
| Disputes | **Open to anyone with a bond**, as Merkl's are: a disputer locks `dispute_bond` of XCH (production 5 XCH, testnet 0.1 TXCH) against a pending root, which then cannot activate. The guardian or the admin resolves: upheld, the root is revoked and the bond refunded; rejected, the bond is forfeited to the dev multisig and the window restarts (§5.2, §7) |
| Operator rotation | **Delayed and cancellable.** The guardian proposes a new operator key; it takes effect `rotate_delay` blocks later (production 4,608 ≈ one day, testnet 24), and the admin can cancel it until then (§5.2) |
| Delegate fee | Every delegate grant or revoke leaves `delegate_fee` as network fee (production 50,000,000 mojos, testnet 1,000,000), so spending the singleton to invalidate other people's prebuilt bundles is not free |
| Excluded addresses | (2026-10-20) **The burn address never earns** (`0x…dead`, where Forge pools burn their locked LP floor), and a creator may **exclude up to 16 addresses** of their own (the params' `blacklist`). Both are puzzle-enforced at claim (§5.4); the excluded hold no share, so everyone else splits the emission |
| Every holder in every root | (2026-10-20) Each root lists every eligible holder, whether or not they ever claim. Whatever nobody has claimed when the claim window ends goes back to the creator (§5.5) |
| NFT campaigns | (2026-10-20) `NFT_HOLD`: one share per NFT, time-weighted, nothing staked. The target is a minter DID or a `col1` collection (§3, §6.8) |

---

## 1. What Merkl does, and what we keep

Merkl ([docs](https://docs.merkl.xyz/merkl-mechanisms/incentive-mechanisms),
[developers](https://developers.merkl.xyz), contracts `AngleProtocol/merkl-contracts`) is
the incentive layer most EVM DEXs use.

| Merkl | How it works there | FLPCR |
|---|---|---|
| `DistributionCreator.createCampaign(CampaignParameters)` | `{creator, rewardToken, amount, campaignType, startTimestamp, duration, campaignData}`; fee deducted from `amount` (3%); min amount per hour per token | `create` leaf on a **distributor singleton**; the campaign struct is hashed into a campaign id; **3% of the deposit** is paid to the dev multisig in the create spend; campaigns are scheduled by block height |
| Off-chain engine | Every few hours, scores positions (time-weighted liquidity; for CLAMMs, fees/token0/token1 weights, in-range only, boosts, blacklists, forwarders for ALMs) and builds a tree of **cumulative** `(user, token, amount)` | Same model, deterministic and exact (§6). The tree is **cumulative per (campaign, holder)**. It scores LP and CAT holders from chain data, using the Forge replay that already exists |
| `Distributor.updateTree(root)` + dispute | An updater posts a root; it becomes claimable after a dispute period; anyone can freeze it by staking the dispute token (100 EURA); a multisig resolves or revokes | `post_root` leaf, then a **dispute window in blocks** before it becomes active; a guardian can `veto`, and **anyone can `dispute`** with a bond coin of `dispute_bond` XCH, which the guardian or admin `resolve`s (refund if upheld, forfeit to the dev multisig if rejected). The same leaf enforces each campaign's **emission ceiling** on chain (§5.3), which Merkl does not have |
| `Distributor.claim(users, tokens, amounts, proofs)` | Leaf `keccak(user, token, cumulative)`; pays `cumulative − claimed[user][token]`; operators may claim for users | `claim` leaf. Leaf `(campaign, holder, cumulative)`; pays `cumulative − claimed`, with `claimed` kept in a **slot** per (campaign, holder). **Only the holder can claim** (their address's key signs, or a coin at their address sends the claim), or a delegate the holder chose; there is no protocol-wide operator claiming on a holder's behalf |
| Unclaimed rewards | Stay claimable forever; governance can reallocate | Claimable until the leftovers are reclaimed, which is possible from `end + claim_window` (`reclaim` leaf; always paid to the creator) |
| API `api.merkl.xyz/v4` (`/opportunities`, `/campaigns`, `/users/{addr}/rewards`) | Read side for apps and wallets | `/v1/...` with the same three resources (§8) |

Merkl's incentive models carried over:

- **Variable rate (default).** A fixed budget per unit of time, split by time-weighted share; APR
  falls as liquidity grows. This is the only model in v1.
- **Fixed rate / capped APR.** Phase 2, because it needs a price (§6.6).
- **ERC20 holding campaigns.** These map to `CAT_HOLD`.
- **Concentrated liquidity (fees / token0 / token1 / in-range).** Only partly applies. A Forge pool is
  a weighted CFMM with **fungible** LP: every LP has the same range and the same per-reserve mix, so
  token0/token1 weights and in-range filters have nothing to act on. The **fee weight** still
  matters: it rewards liquidity that was present when the pool was used (§6.3).
- **Forwarders (ALMs, wrappers).** LP held by a Forge vault pool's nested LP reserve is passed through
  to that vault's LP holders (§6.4).
- **Blacklist / whitelist / minimum balance.** Kept, and committed into the campaign id.

What we drop: Merkl's per-campaign overrides (editable live campaigns), its signed terms gate, and
governance reallocation. A campaign cannot be edited once created, which is the same rule as a
Forge pool's leaf set.

### Why not CHIP-0051 (Reward Distributor)

CHIP-0051 is the native Chia option on the same action layer. Forge declined it once already for
trading fees (`chip0050ActionLayer.md`): holders must **stake**, locking entries in the distributor,
or a manager sets shares by hand. FLPCR's point is that LP and CATs **stay in the holder's wallet**
and keep working: routable, offerable, usable as collateral. That needs an off-chain scorer. The
correctness of the split rests on a reproducible engine plus a dispute window; the *amount* that can
leave rests on the puzzle (§5.3). A CHIP-0051-backed "staked campaign" can come later as a second
kind, for creators who want the puzzle itself to enforce the split.

---

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **Campaign** | A reward budget on one target (a Forge pool or a CAT) over `[start, end)` blocks |
| **Distributor** | The singleton holding one reward asset (XCH or one CAT) for every campaign paying in it |
| **Engine** | The off-chain scorer (`flpcr/engine.py`); its output is a root |
| **Root** | A posted claim tree: every `(campaign, holder, cumulative)` earned up to the root's `end` |
| **Claim window** | The creator's choice, in blocks after `end`, before which no one can reclaim. Holders can claim during it and after it, until a reclaim lands |
| **Operator** | The key allowed to post roots (a coin at `engine_ph` sends a message) |
| **Guardian** | The key (intended to be a Forge multisig safe) that can veto a pending root, resolve a dispute, and propose a new operator key, which takes effect only after `rotate_delay` |
| **Admin** | The dev multisig (`admin_ph`, normally the dev fee address). It can cancel a pending rotation, resolve a dispute, and reclaim a campaign for its creator after the claim window; the payment goes to the creator |
| **Disputer** | Anyone who locks a bond coin of `dispute_bond` XCH against a pending root. The root cannot activate until the guardian or admin resolves; the bond comes back if the dispute is upheld and goes to the dev multisig if it is rejected |

---

## 3. Campaign kinds

| Kind | `target` | Tracked asset | Fee weighting |
|---|---|---|---|
| `LP_HOLD` (1) | Forge pool launcher id | the pool's LP CAT | yes |
| `CAT_HOLD` (2) | CAT asset id | that CAT | no (time only) |
| `NFT_HOLD` (3) | minter DID launcher id, or a `col1` collection id (§6.8) | the NFTs it names, **one share per NFT** | no (time only) |

`NFT_HOLD` rewards collection holders directly: an address's balance is the number of eligible NFTs it
owns, time-weighted per block like any other balance, and nothing is staked. `create` accepts kinds 1, 2
and 3, and its `(kind == 1) | (fees_bps == 0)` rule already keeps fee weighting to LP. Our router can
restrict which collections it accepts (`RouterRules.nft_collections`; empty means any).

Phase 2 candidates, each a new `Kind` with its own scoring function and none needing a puzzle change:
`SWAP_VOLUME` (pay traders by routed volume, from the same replay) and `VAULT_DEPOSIT`. Gating and
boosting by NFT or CAT holdings is not a kind: it applies to every kind (§6.8).

### 3.1 The campaign struct (what the id commits to)

```
campaign = ["flpcr-campaign-v1", distributor_id, creator_ph, reward_asset, amount,
            start, end, kind, target, params_hash, nonce, fee_bps, claim_window]
campaign_id = sha256tree(campaign)
params = [time_bps, fees_bps, min_balance, sorted(blacklist), sorted(whitelist), forward,
          gates, boosts, boost_cap_bps, fee_window]
          gates  = ([holding, asset, min_count] ...)            holding: 1 = NFT, 2 = CAT
          boosts = ([holding, asset, min_count, bps] ...)
params_hash = sha256tree(params)

fee    = amount * fee_bps / 10000      (floor; fee_bps = 300)
budget = amount − fee                  (what holders can earn)
```

`amount` is the deposit, fee included, as in Merkl's `CampaignParameters.amount`. The rules, checked
by both the reference model (`model.py`, `distributor.py`) and the `create` leaf unless noted, are:
- `creator_ph` is 32 bytes;
- `amount > 0` and `budget > 0`;
- `end > start`, and `h + 32 <= start <= h + max_campaign_length`, where `h` is the height the create
  spend names (no retroactive campaigns, even with `h` understated by up to 31 blocks, and no idle row
  parked far ahead in the campaign table);
- `min_campaign_length <= end − start <= max_campaign_length` (testnet from 16 blocks ≈ 5 min, production
  from 77 ≈ 24 min, up to 365 days);
- `kind` is 1, 2 or 3; `time_bps + fees_bps = 10000`, and `CAT_HOLD` or `NFT_HOLD ⇒ fees_bps = 0`;
- `blacklist` (the **excluded addresses**) holds at most `MAX_EXCLUDED = 16` entries, each 32 bytes. The
  leaf stores `excluded = sha256tree(blacklist)` in the campaign, and the blacklist is part of
  `params_hash`, so the campaign id commits to it. A claim reveals the list and is refused for anyone
  on it (§5.4);
- `blacklist ∩ whitelist = ∅`: reference model and router only. The leaf never parses the whitelist; it
  commits to it through `params_hash`;
- `fee_bps` equals the distributor's: the leaf takes it from its `Config`, so the id commits to it;
- `3 · dispute_blocks + 32 <= claim_window <= max_campaign_length` (testnet floor 56 blocks, production
  146). The floor is technical only (§5.5); the creator otherwise picks freely.

The full `params` value sits in the `create` solution, so it is on chain and recoverable from the hash.
The leaf reads `time_bps, fees_bps, min_balance, blacklist, ...rest` (the order of `Params.as_clvm`) and
checks only the split and the blacklist; `model.py` alone checks the rest, including
`fee_window >= 1` (blocks, default 4608 = one day; §6.3). `Params.from_clvm` rebuilds params from a deploy
record (without gates or boosts), so a campaign read back keeps its blacklist and its id.

---

## 4. Architecture

```
 creator ──spend──▶ responder ──create──▶ ┌──────────────────────────────┐
                                          │ Distributor singleton        │
 chain ──▶ Engine ──root manifest──▶ IPFS │  action layer, 10 leaves     │
            │                             │  reserve (upstream finalizer)│
            └──post_root (operator)─────▶ │  state: campaigns, roots,    │
 anyone ──dispute (bond coin)───────────▶ │         dispute              │
 guardian ──veto / resolve / rotation───▶ │  claim + delegation slots    │
 admin ──cancel rotation / resolve──────▶ │  bond coin (nonce 3, XCH)    │
 holder / delegate ──claim(proofs)──────▶ └──────────┬───────────────────┘
 creator / delegate / admin ─reclaim (after window)─▶│ CREATE_COIN, hinted
                                                     ▼
                                          holder's or creator's wallet
```

**One distributor per reward asset.** The distributor is
`singleton(action_layer(reserve_finalizer, leaves_root, state))` with **one** reserve. That is the
upstream CHIP-0050 reserve finalizer **unmodified**. Forge needed a multi-reserve finalizer; this
doesn't, which keeps it out of the audit scope. Every campaign paying in, say, wUSDC goes to the
wUSDC distributor. A registry like Forge's (key = `(reward asset id, router_ph)`, sorted slots, one
distributor per asset per router) lets wallets and the API find a distributor cold.

**One router per distributor.** `create` needs a mode-23 message from a coin at the distributor's
curried `router_ph`, whose content is `"c"` + the campaign id. So a distributor only ever holds
campaigns its operator has agreed to score, and nobody can fund a campaign that no engine watches. It
also needs the creator's own consent: a coin at the creator's address must be spent in the same bundle
and send `"k"` + the campaign id, so whoever builds the bundle cannot name someone else as the creator
who receives the leftovers. Today there is one operator (us): our router, engine and front end.
Running a second operator later means launching another set of distributors with a different
`router_ph`, `engine_ph`, `guardian_ph` and `admin_ph`, with no change to the puzzles. Claims and
reclaims never involve the router; they work even if the operator disappears (§7).

---

## 5. The distributor (on chain)

Rue, CHIP-0050 action layer, upstream puzzles hash-pinned in `contracts/pins.json`, to the same hashes Forge V16 pins (`contracts/v16/pins.json` in forge-puzzles).
Leaf-by-leaf detail is in [`PUZZLES.md`](PUZZLES.md). **`flpcr/distributor.py` is the same rules
as running Python**: the reference the puzzles are tested against, and what the engine's output is
pushed through in `tests/test_distributor.py`.

### 5.1 Config (curried, so the leaf root commits to it) and state

```
Config  = [ distributor_id, reward_asset, fee_bps = 300, dev_fee_ph, admin_ph, guardian_ph, router_ph,
            dispute_blocks, rotate_delay, delegate_fee, min_campaign_length, max_campaign_length,
            max_campaigns, slot_1st_curry_hash, delegation_slot_1st_curry_hash,
            dispute_bond, bond_puzzle_hash, bond_launcher_hash ]
State   = [ reserve, engine_ph, initialized, campaigns, active, pending, rotation, dispute ]
Campaign (in state) = [ id, creator_ph, budget, start, end, claim_window, claimed, excluded ]
                      excluded = sha256tree(params.blacklist)
Root     = [ root_id, start, end, root, totals: ((campaign_id . cumulative_total) ...), activates_at ]
Rotation = [ engine_ph, effective_at ]          (rotation is nil when none is pending)
Dispute  = [ root_id, disputer_ph, bond_parent ] (dispute is nil when none is open)
Claim slot      = (key . (claimed . (left . right))),           key = sha256tree((campaign_id . puzzle_hash))
Delegation slot = (key . (active . (uses . (left . right)))),   key = sha256tree((owner . delegate))
```

These are the exact layouts in `contracts/puzzles/flpcr/shared.rue` (proper lists; the slots are
dotted). `admin_ph` is the dev multisig, normally the same address as `dev_fee_ph`. `dispute_bond` is
mojos of XCH whatever the reward asset (production 5 XCH = 5,000,000,000,000 mojos, testnet 0.1 TXCH =
100,000,000,000); `bond_puzzle_hash` is the distributor's `p2_delegated_by_singleton` at nonce 3, where a
bond sits; `bond_launcher_hash` is the one puzzle a bond coin may be born from (`bond_launcher.rue`).
`Dispute.bond_parent` is that launcher's coin id. `init` checks the `Config` once:
`rotate_delay > dispute_blocks`, `delegate_fee >= 0`, `dispute_bond > 0`, and `bond_puzzle_hash`,
`bond_launcher_hash`, `admin_ph` and `dev_fee_ph` are 32 bytes.
`reserve` is the first state field, so the upstream reserve finalizer's default amount program `(f State)`
sizes the reserve coin and no custom program is needed. The invariant is
`reserve = Σ (budget − claimed) over campaigns`: the reserve holds exactly what campaigns still owe. Conservation is the reserve's job, and honesty about this sum is each
leaf's job (the Forge rule, `chip0050ActionLayer.md`).

### 5.2 Leaves (fixed forever per distributor)

| Leaf | Who | Does |
|---|---|---|
| `init` | anyone, once | Checks the `Config` once (§5.1) and creates the sentinel slots of **both** sorted lists, claims (nonce 1) and delegations (nonce 2), with keys `0x00…` and `0xff…`; `initialized` makes it one-shot, because a second pair would let one key be inserted twice. `create` refuses until it has run, and claims and delegations need its sentinels |
| `create` | the router (mode-23 message `"c" + campaign_id` from a coin at `router_ph`) **and** the creator (`"k" + campaign_id` from a coin at `creator_ph`, by message only; a signature is refused) | Requires `initialized == 1` and a 32-byte `creator_ph`. Validates the struct (§3.1) and derives the id; **prepends** the campaign with `claimed = 0`; `reserve += budget`, and the reserve pays `fee` to `dev_fee_ph` (hinted) **in the same spend**: the dev fee, in the reward asset. The deposit is enforced by **conservation**, not by naming a coin: the reserve is recreated `budget` larger and also pays out `fee`, so the bundle (for a CAT, the reserve's CAT ring) must supply the whole `amount` or it is invalid. For XCH any excess is ordinary bundle slack (network fee); a CAT ring carries no slack, so a CAT deposit coin returns any excess as a CAT change output |
| `post_root` | operator (mode-23 message `"p" + sha256tree([root_id, start, end, root, totals, manifest_hash])` from `engine_ph`) | Sets `pending`. Checks §5.3. `activates_at = h + 32 + dispute_blocks`, so a root is claimable at least `dispute_blocks` after the block that includes it |
| `veto` | guardian (mode-23 message `"v" + sha256tree(root_id)` from `guardian_ph`) | Clears `pending` while it is still in its dispute window. Refused while a dispute is open: a disputed root is settled by `resolve` |
| `dispute` | **anyone**; no authorization. The bond is the authorization | `[h, root_id, disputer_ph, bond_grandparent]`. Requires a pending root with that id, no open dispute, a 32-byte `disputer_ph` (where a refund goes; the disputer's choice) and a nonzero `bond_grandparent` (the disputer's own coin). Derives the bond launcher `bond_parent = coinid(bond_grandparent, bond_launcher_hash, dispute_bond)` and asserts its coin announcement `sha256(bond_parent + "flpcr-bond-v1" + sha256tree([bond_puzzle_hash, dispute_bond, distributor_id]))`. The launcher (created by the disputer's own coin spend with exactly `dispute_bond` mojos of XCH, hinted with the distributor id) creates the bond coin at `bond_puzzle_hash` for its whole amount and announces it; a coin id commits to its puzzle, so a bond that was never funded, or funded with the wrong amount, cannot open a dispute (the Forge registry's reserve-launcher pattern). Records `dispute = (root_id, disputer_ph, bond_parent)`; the root stays pending and the prologue will not promote it while the dispute is open |
| `resolve` | **the guardian or the admin** (mode-23 message `"s" + sha256tree([root_id, upheld])` from a coin at `guardian_ph` or `admin_ph`) | `[h, upheld, sender]`, `upheld` 0 or 1, with a dispute open. **Upheld:** `pending = nil` (the operator reposts a corrected root under the same id) and the bond is refunded to `disputer_ph`. **Rejected:** the bond is forfeited to `dev_fee_ph` and `pending.activates_at = h + 32 + dispute_blocks` (the window restarts). Either way `dispute = nil`, and the leaf sends a mode-23 message to the bond coin `coinid(bond_parent, bond_puzzle_hash, dispute_bond)` carrying the tree hash of the one delegated puzzle `(q . [[51, recipient, dispute_bond, [recipient]]])`; the bundle must spend the bond coin with exactly that puzzle, and nothing else can be run against it |
| `claim` | **the holder**, by key signature (`AGG_SIG_UNSAFE` of the leaf by the synthetic key whose standard puzzle hash is `ph`) or by message (mode-16 `"k" + leaf` from a coin at `ph`), **or a delegate the holder granted**, the same two ways at the delegate's address, plus the holder's delegation slot. Neither form names a generation (§5.4) | Requires a 32-byte `ph` that is not the burn address, and the campaign's excluded list (revealed in the solution, checked against `campaign.excluded`) must not contain it. Against the **active** root, for any campaign not yet reclaimed (no deadline): verifies `(campaign, ph, cumulative)`, pays `cumulative − claimed` from the reserve to `ph` (hinted), and records `cumulative` in the claim slot |
| `reclaim` | **the creator** (signature or message over `sha256tree(["flpcr-reclaim-v1", distributor_id, campaign_id])`, or a delegate the creator granted, by signature or message), **or the admin** (`auth = 3`: a coin at `admin_ph` sends `"k"` + that message), from the campaign's `end + claim_window` | Pays `budget − claimed` to `creator_ph` (hinted), whoever authorized it, and removes the campaign |
| `rotate` | `cancel = 0`: the guardian (`"r" + new_engine_ph`); `cancel = 1`: the admin (`"x" + engine_ph` of the pending rotation) | `cancel = 0` stores `rotation = (new_engine_ph, h + 32 + rotate_delay)`; a newer proposal replaces an older one and restarts the delay. The prologue adopts it as `engine_ph` on the first spend at or after `effective_at`; until then the current operator keeps posting. `cancel = 1` clears a pending rotation naming that key. The new key must be 32 bytes |
| `delegate` | the owner (signature or message over `sha256tree(["flpcr-delegate-v2", distributor_id, owner, delegate, grant])`) | `owner` and `delegate` are 32 bytes; `grant` is 0 or 1. The first grant for a key inserts `(key, 1, 0, left, right)` between its neighbours; a later grant or revoke spends `(key, active, uses, left, right)` and recreates `(key, grant, uses + 1, left, right)`. A revoke of a never-granted key is refused. Emits `RESERVE_FEE delegate_fee`. State is unchanged |

Every message except a claim's is mode 23: sender identified by puzzle hash, receiver by **coin id**,
the current singleton coin. Such a message is therefore bound to one generation of the distributor and
cannot be replayed against a later one. A claim's message is mode 16 (receiver uncommitted) and a
claim's signature is `AGG_SIG_UNSAFE`, deliberately generation-free; §5.4 says why replay is harmless.

**The height pin.** Every solution starts with `h`. The first action of a spend asserts
`ASSERT_HEIGHT_ABSOLUTE h` and `ASSERT_BEFORE_HEIGHT_ABSOLUTE h + 32` and stores `h` in ephemeral
state; every later action in the same spend must name the same `h`. Without that last rule a second
action could claim a later height than the first, for example to reclaim before the window opens.
The mutation run found the test that proves it was missing (§12).

`h` may trail the block that includes the spend by up to 31 blocks (`HEIGHT_WINDOW = 32`). So every rule
that counts blocks from `h` toward something another party relies on adds 32: the dispute window
(`activates_at`), the rotation delay (`effective_at`) and a campaign's earliest `start`. Understating `h`
can never shorten any of them.

There is no upgrade leaf and no "withdraw everything" leaf. A compromised operator and a
compromised guardian together can still pay out only up to each campaign's emission schedule
(§5.3), and only after a dispute window. The guardian alone cannot make itself the operator quickly:
a rotation waits `rotate_delay` in public state, and the admin can cancel it.

**Pending becomes active lazily.** The prologue of whichever spend comes next promotes `pending`
to `active` once `h >= activates_at` and no dispute is open, and adopts a pending rotation once
`h >= effective_at`. A disputed root waits for `resolve`, however long that takes; an unresolved
dispute therefore freezes posting (`post_root` needs no pending root) but never claims against the
previous active root. A new
root cannot be posted while one is still in its dispute window, which is Merkl's `updateTree` rule.
Because promotion waits for a spend, a root is active only once a spend has promoted it: if nobody
spends the singleton in between, the guardian's veto can still land up to 31 blocks after
`activates_at` (its `h` may trail the tip).

### 5.3 The emission ceiling (the on-chain guarantee)

`post_root`'s solution carries `(root_id, start, end, root, totals, manifest_hash)`, where
`totals[c]` is the sum of campaign `c`'s cumulatives in this tree. The leaf asserts:

```
root_id == last.root_id + 1     start == last.end (any for the first)     start < end <= h
for each live campaign c in the last root's totals:  c is in totals                (never dropped)
for each (c, t) in totals:  c is live;   t >= last.totals[c];
                            t <= budget_c · (min(max(end, start_c), end_c) − start_c) / (end_c − start_c)
```

The right-hand side is `Campaign.emittable(end)` in `model.py`, which the engine checks itself before
posting. So **no key can release a campaign's money faster than its schedule, or past its budget**,
whatever tree it posts. `claim` also refuses to take a campaign's `claimed` past its `totals` entry
in the active root. A tree whose leaves add up to more than its stated totals cannot pay its last
claims, so understating totals gains nothing.

What the puzzle does **not** guarantee: that the split between holders is correct. That is the
engine's job, made checkable by determinism (§6) and enforceable by the dispute window (§7).

### 5.4 Claims: cumulative, at any time

As in Merkl, a holder claims whenever they like: every root, once at the end, or anything in between.
Each root carries the full cumulative, so a holder who skips ten roots claims once and gets all ten.
Claiming from an older root after a newer one is active is refused, because only the active root
verifies; the newer one includes everything the older one did.

`claimed` lives in a **slot** per (campaign, holder), because it grows without bound and is read only
by that holder's claim, which is the CHIP-0050 slot use case. The first claim must prove that no slot
exists for its key. FLPCR does this the way CATalog does, with a **sorted, doubly linked list of
slots between sentinels**: a first claim for key `k` spends its left neighbour `L` and right neighbour
`R`, whose values name each other (`L.right = R.key`, `R.left = L.key`), so the adjacency is structural:
a slot with the wrong pointers does not exist. It asserts `L.key < k < R.key` and recreates `L` and `R`
pointing at a new slot for `k`. **The ordering assertion is what stops a double claim.** Without it, a
holder could insert a second slot for their own key between two other neighbours, starting again from
`claimed = 0`. A later claim spends only the holder's own slot. Slots are hinted with their key, so the
API finds a holder's slot without knowing how much they have claimed. Claims serialize on the
singleton anyway (each one pays from the reserve), so neighbour contention costs nothing extra.
Several claims, for several holders and campaigns, batch into one singleton spend, as long as no two
of them touch the same claim or delegation slot (a shared neighbour counts: two first claims whose
keys fall between the same two slots collide) and no two pay the same amount to the same puzzle hash
(one coin id twice). The driver refuses such a batch with `BundleCollision`; the sequencer (below)
splits it across chained generations.

**The burn address and excluded addresses never claim.** A claim's solution ends with `excluded`, the
campaign's excluded list. Before the Merkle proof is checked, the leaf asserts, in this order:

1. `ph != BURN_PUZZLE_HASH` (`0x…dead`, Chia's burn address, where Forge pools burn their locked LP floor);
2. `sha256tree(excluded) == campaign.excluded`, the hash `create` stored, so the list cannot be shortened;
3. `ph` is not in `excluded`.

A claim leaves `excluded` unchanged in the updated campaign. The engine gives these addresses no share in
the first place (§6.7), so the checks are a backstop against a wrong root, not the split itself. The
driver records each campaign's list at `create()` and passes it to `claim(..., excluded=None)` by default.

**Only the holder can claim their own rewards, and no claim names a generation.** A claim must carry
one of these authorizations (`authorize_claim` in `shared.rue`), and the puzzle refuses anything else:

| `auth` | Who it suits | What the puzzle checks |
|---|---|---|
| `0` signature | a standard wallet address (Sage's normal addresses), which needs no coin | `auth_data` is a synthetic public key whose standard puzzle hash (`curry_tree_hash(STANDARD_MOD_HASH, [pk])`) is `ph`; the leaf emits `AGG_SIG_UNSAFE(pk, leaf)`. A wallet signs the claims of all its addresses in one approval (Wallet experience, below) |
| `1` message | every address: a standard wallet, a vault, a Forge multisig safe, any other puzzle | `RECEIVE_MESSAGE` mode 16 (`MODE_PUZZLE_TO_ANY`: sender by puzzle hash, receiver uncommitted) of `"k" + leaf` from a coin whose puzzle hash is `ph`. Only whoever controls that puzzle can make that coin spend. One send still matches exactly one receive: two receivers of the same message fail with `MessageNotSentOrReceived` (checked on the simulator) |
| `2` delegate | a delegate the holder granted | `auth_data = (delegate, uses, left, right, auth, data)`. The holder's active delegation slot for that delegate is spent and recreated with `uses + 1`, and the delegate authorizes the leaf by signature or by message, as above |

The authorized message is the claim leaf itself, `sha256tree(["flpcr-claim-v1", distributor_id,
campaign_id, ph, cumulative])`, so an authorization names exactly one claim on exactly one
distributor, and no other distributor accepts it. Neither form names the singleton coin
(`AGG_SIG_ME` is no longer used by claims), so an authorization built against one generation lands
in any later one. **Replaying it pays nothing:** the claim slot records `claimed = cumulative` and
the leaf asserts `amount > 0`, so the same leaf a second time is refused. The payout always goes to
`ph`, so whoever submits a claim, a sequencer included, can never redirect it. The rule is about who
decides when rewards move.

Every other action (create, reclaim, delegate, rotate, post_root, veto, dispute, resolve) is
unchanged: mode 23 and `AGG_SIG_ME` where it used them, and `create` still needs the creator's own coin
spend. **Owner rule FLPCR-07** ("to claim a person has to spend a mojo") was puzzle-enforced for
claims until this change (a bare signature was refused). It is now met off-puzzle: the claimer's
wallet makes the one fee-paying coin spend of the claim bundle. This change is **pending the owner's
confirmation**.

**A delegate the owner chose.** A holder can let one specific other address claim for them: a cold
wallet whose hot wallet (or a cloud wallet we run for them) approves claims, or a vault whose signer
does. A creator can do the same for reclaims. This is Merkl's operator role, except the holder picks
the operator, and there is no protocol-wide one.

1. **Grant, once.** The owner authorizes `delegate` with grant 1, by signature or by message from its
   own address (§5.2), and the bundle leaves `delegate_fee` as network fee. A vault or multisig grants
   by message and never has to act on a claim again. Delegations are their own **sorted, doubly linked
   slot list** (nonce 2, so a delegation slot can never be read as a claim slot), one slot per key
   `sha256tree((owner . delegate))`, hinted with the key. The first grant inserts `(key, 1, 0, left,
   right)` between its neighbours, the same way a first claim does.
2. **Claim, any number of times.** `auth = 2`, `auth_data = (delegate, uses, left, right, auth, data)`. The
   claim spends the slot `(key, 1, uses, left, right)` and recreates it as `(key, 1, uses + 1, left,
   right)`, which proves the grant is active, and the delegate's key signs the leaf or the delegate's
   coin sends `"k" + leaf`. The two values
   always differ, so a slot the spend itself creates can never prove its own grant. The payout goes to
   the holder.
3. **Revoke.** The owner authorizes grant 0 (and pays `delegate_fee` again); the slot is recreated with
   `active = 0` and `uses + 1`, and the delegate's next claim finds no active slot and is refused. The
   holder can always still claim directly.

The rules the tests pin:
- **Only the owner grants or revokes:** a delegate cannot grant itself, and a stranger's message
  cannot grant for an address.
- **Only the granted delegate claims:** another address, even one spending a coin of its own, is
  refused; the granted delegate claims by message or by key signature.
- **One hop:** a delegate cannot delegate.
- **Grant is exactly 0 or 1;** a revoke of a never-granted key is refused.
- **One key, one slot:** granting twice and revoking once leaves the delegate revoked.
- **Every grant or revoke pays `delegate_fee`** as network fee.
- **Rewards go to the holder:** the delegate's balance never changes. A grant made once serves every
  later claim, across roots.

**Wallet experience.** The interface builds the claim bundle; the holder approves it in Sage. For
standard addresses that is one signing request (`chip0002_signCoinSpends`) for a never-pushed,
zero-amount stand-in spend whose puzzle is `(q . ((49 key leaf) ...))`, one `AGG_SIG_UNSAFE` per
address that has a leaf, the same pattern as Forge's fee proof. The signature it returns can only
authorize those claims. One approval covers every address of the wallet, and none of them needs a
coin; the wallet's one fee-paying coin spend (any of its coins) rides in the same bundle. A vault,
multisig or other puzzle claims by message, with a coin at the rewarded address.

#### Sequencing claims

Because no authorization names a generation, a **sequencer** can collect claims from many wallets
and pay them together. `Distributor.sequence(h, requests, max_cost=5_000_000_000,
per_generation=25)` takes `ClaimRequest(campaign_id, puzzle_hash, cumulative, proof, auth)` items and
returns a `ClaimBatch`:

- **One request per (campaign, holder);** the highest cumulative wins.
- **Generations in one bundle.** A generation takes every claim whose slots no earlier claim in it
  touches (first claims that share a neighbour in the sorted claim-slot list go to the next
  generation, where that neighbour has been recreated), up to `per_generation`. Further generations
  of the singleton are chained in the **same** bundle: generation N+1 spends the singleton coin, the
  reserve and the slots generation N created.
- **A cost budget.** Generations are added while the consensus cost estimate (`spend_cost`: CLVM +
  12,000 per byte + 1,800,000 per `CREATE_COIN` + 1,200,000 per `AGG_SIG_*`) stays under `max_cost`;
  the rest is `deferred` to the next bundle. Requests the leaf refuses are `rejected` without sinking
  the batch.
- **The driver is left unchanged;** `apply_batch(batch)` advances it once the bundle confirms.
  `reloaded()` returns the distributor in a fresh CLVM allocator (the SDK's allocator never frees, so
  a long-running sequencer swaps between bundles).

Measured with `scripts/scale_claims.py` on the simulator, every holder a real BLS wallet claiming by
key signature:

| Holders | Engine | Proof depth | Bundles | Result |
|---|---|---|---|---|
| 1,000 | 1.5 s (4,000 balance events) | 10 | 9 | every holder paid exactly what the root says |
| 5,000 | 8.0 s (20,000 events) | 12 | 42 (94 generations), ~1 s to build each | every holder paid exactly; the reserve kept only rounding dust (2,497 mojos) |

A first claim costs about 38.7M (two neighbour slot reveals, three new slots, the proof and the
payout), whatever the generation's size, so about 125 first claims fit a 5B-cost bundle. At one bundle
per transaction block (~52 s) that is about 8,600 claims an hour: 5,000 holders all paid within about
36 minutes.

### 5.5 Leftovers go back to the creator

The **creator sets `claim_window` when creating the campaign**. It is part of the struct and the id, so
anyone reading the campaign sees every term: budget, schedule, scoring, and how long holders are
guaranteed to have after the end. For each campaign:

- `reclaim` is refused before `end + claim_window`. From then on it can be submitted by the creator
  (their signature, or a message from their address for a vault or multisig creator), by a delegate the
  creator granted with the same `delegate` leaf (a hot wallet for a cold-wallet creator; a holder's
  delegate is not the creator's), or by the **admin**, the dev multisig, so a fully claimed or abandoned
  campaign cannot hold a table slot forever. Whoever submits it, it pays `budget − claimed` **to the
  creator** and removes the campaign (its claim slots stay as inert 0-mojo coins).
- `claim` has **no deadline**. It works for as long as the campaign exists, so a holder who comes back
  after the window, before the creator has reclaimed, is still paid in full. The window guarantees
  holders a minimum; it never takes anything from them early.
- A claim and a reclaim racing in the same block are serialized by the singleton: whichever spend
  lands first wins, and the other is rebuilt against the new state (the late claim then finds the
  campaign gone; the late reclaim pays out less).

The claim window's only floor is technical: at least `3 × dispute_blocks + 32` (testnet 56 blocks,
production 146). That leaves room for the previous root's dispute window, one veto and repost of the
root covering `end`, and the height slack, before reclaiming opens. Its ceiling is the longest
campaign, 365 days.

**The reclaim guard (operator policy, not a puzzle rule).** By design, unclaimed rewards return to the
creator from `end + claim_window`. But the puzzle counts the window from `end`, not from when the final
root became claimable, so a reclaim can land while holders are still waiting on that root. Our tools
refuse to reclaim (`reclaim_blockers()` in `scripts/deploy_testnet.py`; `--force` overrides) while:

- the campaign's emission is not fully posted (the last root ends before the campaign does);
- a root carrying the campaign is still pending;
- the final root became claimable less than `claim_window` blocks ago and holders still have unclaimed
  amounts in it.

A front end's Reclaim button should apply the same checks. A script once nearly reclaimed a whole unclaimed
root on testnet; `tests/test_reclaim_guard.py` pins that case among its five.

### 5.6 Campaign length and claim cadence

The distributor bounds **campaign length** (`end − start`):

| Preset (`Config` in `distributor.py`) | `dispute_blocks` | `min_campaign_length` | `max_campaign_length` | minimum `claim_window` | `rotate_delay` | `delegate_fee` |
|---|---|---|---|---|---|---|
| `Config.testnet(...)` | 8 (≈ 2.5 min) | 16 (≈ 5 min) | 365 days | 56 | 24 | 1,000,000 mojos |
| `Config(...)`, production | 38 (≈ 12 min) | 77 (≈ 24 min) | 365 days | 146 | 4,608 (≈ 1 day) | 50,000,000 mojos |

Both presets also set `max_campaigns = 32`. `Config` is the single configuration class: the driver
curries the leaves from it, and it refuses the same bad values `init` does.

Within a campaign, holders claim **at any interval they choose**. The engine posts a root as soon as the
previous one is active, so a fresh root becomes claimable about every `dispute_blocks + 32` blocks, and
a holder can claim after each one, once at the end, or anything between. Each claim bundle costs the holder a
network fee (one coin spend of their wallet); whether frequent small claims are worth it is up to them. The interface
shows the claimable amount next to the estimated fee.

A 24-minute campaign is short, and the operator's cadence has to fit it. The engine must post the root
covering `end` within a few blocks of `end`, and the guardian's watcher must veto within the dispute
window. Both are bots, not people: a 12-minute dispute window is too short for a human guardian to react
to, and the automated watcher is what makes it safe.

`budget − claimed` covers both kinds of leftover: budget **nobody earned** (stretches with no eligible
holder, floor dust) and earnings **nobody claimed**. The operator's duty is to post the root covering
`end` early enough that it activates well inside the window. A watcher alerts if
`end + dispute_blocks + 32` passes with no such root.

---

## 6. The engine (off chain): normative scoring

Implemented in `flpcr/engine.py`. A second implementation must produce byte-identical roots. All
arithmetic is exact rational; the only rounding is a floor of a holder's cumulative accrual.

### 6.1 Inputs, all from chain

- **Holder timeline** of the tracked asset: every CAT coin creation (+amount) and spend (−amount),
  attributed to its inner puzzle hash via the CHIP-0020 hint (Forge LP coins are hinted). A change at
  height `h` is in force on `[h, …)`.
- **Live LP holders** (`flpcr/lp_crawl.py`, `flpcr/live_source.py`). Every V16 LP mint goes through an
  eve coin at one fixed puzzle hash per pool, `CAT(LP asset, LP_MINT_INNER_HASH)`, pinned by V16.
  `LpCrawl` starts from that puzzle hash's whole coin history and walks every LP coin forward, reading
  each spend's child inner puzzle hashes with `Puzzle.parse_child_cats`. It is incremental, cached in
  the gitignored `lp-crawl-<network>.json`. `LiveLpSource` takes the pool → LP asset map from the Forge
  responder's `/api/pools` (`lpCatAssetId`). On all 8 V16 testnet pools, crawled holdings plus the 1-LP
  burned floor equal each pool's `total_lp` exactly; the first crawl took 16 s, later updates under 1 s.
  It serves holder events only for now, so campaigns on it are time-share only (`[10000, 0]`).
- **NFT holdings** for `NFT_HOLD` (`Source.nft_events`): one event per NFT changing owner, read from the
  DID's own spends, filtered to a `col1` collection by verified metadata (§6.8). `ChainNftSource` reads
  them through any `ChainReader`: the simulator, or `flpcr/chain/node_reader.py` `NodeReader`, which
  answers the same three calls (`coin_state`, `children`, `coin_spend`) from a full node or coinset.org,
  with caching. `Combined` joins it to an LP source.
- **Exclusions:** pool reserve puzzle hashes, the LP mint and melt inner puzzles, OFFER_MOD settlement
  coins (transient, created and spent within a route), the burn address `0x…dead` (the `LOCKED_BURN`
  floor) and the dev fee address. They are derived from the Forge build and registry, never configured
  by hand. The burn address is excluded from NFT holders too.
- **Fee events** for `LP_HOLD`: each pool generation is replayed with `forge_resync`, which picks the lane by
  protocol (V16 = 17; V15 and V14 pools stay live). Resolve each action's leaf through its selector, not
  `zip(puzzles, solutions)` (`forge_v16_resync.py`; first written up in `forgePuzzleV14.md` §5). Each
  `swap` action yields `(h, weight)`, where weight is the LP fee it paid, valued in XCH at the pool's
  **pre-spend** spot price (the price the oracle accumulates in the prologue). The valuation is an
  **exact rational**: the fee in mojos times the spot price as the ratio the pool's integer reserves
  (and weights) define, `Fraction(...)`, never a float, so every replay gets the same weight. A fee paid
  in XCH is its mojos. The `Source` boundary accepts an `int` or a `Fraction` and refuses a float
  (`flpcr.sources.fee_weight`); in JSON a weight is an integer or `[numerator, denominator]`.
- **Address links** for gates and boosts: the operator's append-only link log (§6.8).
- **Forwarders:** Forge vault pools whose reserves include this pool's LP, mapped to the vault's own LP
  asset.

### 6.2 Time weighting (variable rate)

Take the root's window `[a, b)` clipped to the campaign and cut it at every balance, gate, boost or link
change into segments `[s, t)`. With `E(h) = campaign.emittable(h)` and eligible balances `B_u`:

```
budget(s,t) = (E(t) − E(s)) · time_bps / 10000
credit_u   += budget(s,t) · B_u / Σ B
```

Each block's emission is split by share of eligible supply, the same as Merkl's variable rate and
MasterChef. A segment with no eligible holder credits no one, and its budget comes back to the creator
at reclaim. Splitting a segment in two splits its budget exactly (`E` telescopes), so time credit does
not depend on where roots are cut.

### 6.3 Fee weighting (`LP_HOLD` only)

The fee share is settled per **fee bucket**, a fixed, campaign-relative stretch of `W = fee_window`
blocks (a campaign param, default 4608 = one day): bucket `k` is
`[start + k·W, min(start + (k+1)·W, end))`. Each event's LP share is averaged over the `W` blocks that
end at the event (`[h_e − W + 1, h_e]`, clipped at height 0, reaching back before the campaign's start):

```
share_u(h)  = Σ_{x in (h−W, h]} B_u(x)/ΣB(x)  /  (number of those blocks with ΣB(x) > 0)
budget(k)   = (E(be) − E(bs)) · fees_bps / 10000              for bucket k = [bs, be)
credit_u   += budget(k) · Σ_e w_e · share_u(h_e)  /  Σ_e w_e   over events e in bucket k
```

`B` is the eligible weight of §6.2 (gates, boosts, links, lists and minimum applied), and each event `e`
counts only if its `share` is non-empty. A bucket with **no** such event spends its fee share on time
weighting over that bucket (§6.2 with `fees_bps` in place of `time_bps`), so a quiet pool doesn't
strand the budget. This is the fungible-LP form of Merkl's `propFees`: an LP's fee share in a block *is*
their LP share, so weighting by fees moves rewards toward the days when the liquidity was used.

- **A bucket is credited once it has closed:** a root `[a, b)` adds the fee share of every bucket whose
  end lies in `(a, b]`, and the time share of every block in `[a, b)`. So the exact accrual at a height
  depends on the chain alone; any set of root boundaries gives the same cumulatives at the heights they
  share. Root boundaries are still posted on chain (`start`, `end`), but they no longer move value.
- **Why average shares, not balances:** each block contributes at most `1/W` of a share, whatever the
  size of the balance in it. A deposit held for one block (the attack in review FLPCR-01: add LP, swap
  dust, leave) earns one block's share of the fee weight, not its LP share of the whole fee budget.
- **Why one day:** the lookback must be long next to a single block, so a flash deposit is worth about
  `1/W` of a swap (`1/4608`); a creator who wants fee weighting to follow activity more closely can pick
  a shorter `fee_window`. The bucket shares the length so one number describes the rule, and so the fee
  half of the budget is never more than a day behind the time half.

### 6.4 Forwarding

A wrapper's balance `W` at height `h` is credited to its share holders `p` as `W · S_p / ΣS`, with the
share asset's own exclusions applied. Forwarding is one level deep: a wrapper of a wrapper is credited
to the inner wrapper's puzzle hash. A campaign disables it with `forward = 0`. A wrapper on the
campaign's blacklist is not forwarded: its balance stays with the wrapper, which the blacklist keeps from
earning, so blacklisting a vault excludes its LP rather than passing it to the vault's holders. NFTs
have no wrappers: an `NFT_HOLD` campaign (`Holdings(..., nft=True)`) reads `Source.nft_events` and skips
forwarders.

### 6.5 Accrual and the claim tree

```
accrued[c][u]    += credit_u                (exact, kept across roots)
cumulative[c][u]  = floor(accrued[c][u])    (never decreases)
totals[c]         = Σ_u cumulative[c][u]    (≤ E(end), so §5.3 always accepts it)
```

The tree holds one leaf for every `(c, u)` with `cumulative > 0`, sorted by `(campaign_id, puzzle_hash)`:

```
sha256tree(["flpcr-claim-v1", distributor_id, campaign_id, puzzle_hash, cumulative])
```

It is a Chia `merkle_utils` tree (leaf prefix `0x01`, node `0x02`, midpoint split, proof
`(path, hashes)`), which `simplify_merkle_proof` verifies on chain. A window in which nothing claimable
changed is not posted (`run_epoch` returns no root), and the next run covers the longer window, so posted
roots tile the timeline. A campaign that has been reclaimed is left out of the engine's input and drops
out of later trees.

Each root manifest also commits the address link log the root used (§6.8): `links.count`, the number
of log entries with heights below `end`, and `links.sha256`, sha256 of that prefix's canonical JSON. The
engine state carries the same pair, and the engine refuses to build the next root if the log no longer
starts with that prefix or gains an entry dated before the last root's `end`.

### 6.6 Valuation (display only in v1)

APR and TVL in the API use the pools' oracle accumulators (`observe` snapshots and the cumulative price
in state) for a TWAP, not a spot price that can be moved. In v1 no reward depends on a price except fee
weights (§6.3), which use the pre-spend spot of the pool that paid them. Fixed-rate / capped-APR
campaigns would make price load-bearing, and must use the TWAP only.

### 6.7 Anti-gaming

Time weighting per block makes a flash deposit worth one block's share. Fee weighting does the same
through the lookback (§6.3): a one-block deposit counts for one block out of `fee_window`, so pairing it
with a dust swap no longer takes the fee half of a bucket. The minimum balance and the blacklist apply.
Self-trading to farm fee weight costs the trader the full pool fee for a share of a **fixed** budget, and
the extra weight goes to the LPs of the trailing day in proportion to their averaged shares, so it pays
only to someone who already held a large share for most of that day. The create form should show
creators that trade-off next to the `fees_bps` slider.

**Custody is nobody's position.** `Campaign.eligible` returns False for the burn address and for every
excluded address, so they hold no share and everyone else splits the emission; the claim leaf refuses
them too (§5.4). The LP crawl drops custody puzzles (mint and melt inner puzzles, offer settlement, the
burn address) before scoring, and the NFT reader drops offer settlements and the burn address.

**A copied collection id earns nothing.** The `col1` id of an NFT campaign binds the minter DID to the
metadata's collection id (§6.8). An NFT another DID minted with the same collection id in its metadata,
an NFT whose metadata file was tampered with or is missing, and the same DID's NFTs from another
collection are all left out.

### 6.8 Token gates and boosts

A creator can restrict a campaign to holders of something, reward them more for holding something, or
both. Each **requirement** is "hold at least `min_count` of `asset`":

| `holding` | `asset` | Counted |
|---|---|---|
| `NFT` (1) | the collection id (below) | NFTs of the collection owned by the puzzle hash |
| `CAT` (2) | CAT asset id | mojos of the CAT held by the puzzle hash |

- **Gates.** A holder's balance counts only while they meet **every** gate. Example: an LP campaign on
  a Forge pool open only to Wizard holders, `gates = [(NFT, wizards_did, 1)]`.
- **Boosts.** While a holder meets a boost's requirement, their weight rises by its `bps`. Boosts stack,
  so tiers are boosts on the same collection at rising counts: `(NFT, wizards, 1, +2000)`,
  `(NFT, wizards, 3, +3000)` and `(NFT, wizards, 10, +20000)` give 1.2x, 1.5x and the cap. The total
  multiplier is capped at `boost_cap_bps` (default 25,000 = 2.5x, Merkl's usual ceiling; at most 10x).
- **A boost reshares, it never adds.** Every block's emission is fixed by the schedule; boosted holders
  take a larger share of it and everyone else a smaller one. The emission ceiling (§5.3) is untouched.
- **Checked block by block**, like balances: requirement changes cut segments the same way balance
  changes do. An NFT borrowed for one block earns its holder exactly one block of gated or boosted share,
  so flash-lending a Wizard is worth almost nothing.
- **After forwarding.** Gates and boosts apply to whoever is credited, so for LP held in a Forge vault
  the vault's own holders are checked, not the vault.
- **Nobody qualifies:** that stretch's budget is unallocated and returns to the creator at reclaim,
  exactly like a stretch with no holders.

Weights are exact: `weight = balance × multiplier / 10000`, inside the same rational arithmetic as §6.2
and §6.3.

**What "collection id" means on Chia.** The collection id marketplaces show (MintGarden, Dexie) comes
from CHIP-0007 off-chain metadata, which anyone can copy into an unrelated NFT. The on-chain identifier
that cannot be forged is the **minter DID**: each NFT in the collection has its own launcher id, and only
the DID's owner can mint into it. So an NFT target (a gate's `asset`, or an `NFT_HOLD` campaign's
`target`) is either:

- **the minter DID's launcher id:** every NFT that DID minted; or
- **a `col1` collection id:** `sha256(DID launcher id in hex + metadata collection.id)`, the id explorers
  show (it matched Spacescan's for the testnet "Forge LP" collection, `col1uraa0n4…`). The collection id
  lives in the CHIP-0007 metadata file, and the chain holds that file's sha256, so it is committed by the
  chain. An NFT counts only if **both** hold: (1) the DID minted it, by the evidence rules below; and (2)
  its metadata file, fetched and checked against the on-chain hash, names the collection id
  (`metadata_collection`; the shape is MintGarden Studio's, `collection: {id, name, attributes}`).
  `ipfs://` URIs resolve through public gateways (`http_fetch`); any gateway will do, since only a file
  with the right hash is accepted. `ChainNftSource(collections={col1: (did, uuid)})` takes the pairs and
  checks each one hashes to its `col1`; `.members(target)` lists the NFTs a target names.

*Built: `flpcr/chain/nfts.py`, tested on the simulator (`tests/test_nfts.py`).* A collection is found by
walking **the DID's own spends**. Nothing but the DID's owner can make a DID coin spend, so an NFT counts
only on evidence from one:
- **Created by a DID spend:** its launcher is a child of a DID spend, or the child of a coin the DID
  spend created that was spent as the standard NFT intermediate launcher (`nft_intermediate_launcher`,
  checked by its revealed mod and the launcher hash curried into it). Any other coin a DID spend creates
  (a payment, a memo or notification coin) belongs to someone else, who could launch anything from it,
  so the walk never follows it.
- **Announced by a DID spend at mint:** a DID spend announced the NFT's launcher id (a 32-byte puzzle
  announcement) and, in the same block, the NFT's eve spend set that DID as its owner. This is the
  reference wallet's DID-authorized mint, whose launcher's parent is an ordinary XCH coin. An NFT minted
  elsewhere that the DID takes ownership of later does not count: its eve was not in the DID's block.

The reader deliberately ignores two weaker signals:
- **The metadata collection id on its own**, which can be copied. It narrows a DID's NFTs to one
  collection; it never adds an NFT the DID did not mint.
- **The NFT's owner-DID field on its own.** An NFT minted elsewhere can later be transferred to the DID,
  so the field does not prove minting.

Each NFT is then followed spend by spend. Its owner is the p2 puzzle hash, which is also its hint, and
each change of owner becomes `−1` for the old owner and `+1` for the new, like a CAT balance. An NFT
spent into something that does not parse as its next generation (a melt, or a spend the reader cannot
follow) ends there: its last owner gets a final `−1` at the spend height. NFTs sitting in an offer's
settlement puzzle are nobody's holding. The tests mint Wizards from one DID and a look-alike from
another, transfer one, and check that the gate counts exactly the Wizards, at the right owners, from the
right height; they also check that a launcher under a DID's payment to a third party is refused and that
a DID-authorized mint from an XCH coin is counted. Two alternatives, for when that does not fit:
- **A fixed list.** For a collection minted without a DID, or a curated subset (e.g. only certain
  Wizards), `asset` is the root of a published list of NFT launcher ids. The list is part of the
  campaign's published terms. *(Not built; the engine's source interface takes it without change.)*
- **Soulbound NFTs** (BOW rewards, `nftRewards.md`) work as is: they cannot be transferred, so the gate
  cannot be lent at all.

**One wallet, many addresses: address links.** *Built: `flpcr/links.py`, tested in `tests/test_links.py`
with real BLS keys.* Sage spreads a wallet across many addresses (puzzle hashes). A holder whose Wizard
sits at one address and LP at another would not match, because requirements are checked per address.
A **link** joins addresses into one holder for gates and boosts only:

- **Both addresses sign** the same message,
  `flpcr-link-v2:<distributor_id>:<primary>:<member>:<seq>` (hex ids, decimal `seq`), in Chia's
  `signMessageByAddress` format (CHIP-0002, UTF-8: the synthetic key signs
  `sha256tree(("Chia Signed Message", message))`). A one-sided link would let anyone attach their LP to
  someone else's Wizard, so the primary must consent too. The distributor id scopes the signature to
  one distributor.
- **Either address can unlink** with one signature over
  `flpcr-unlink-v2:<distributor_id>:<primary>:<member>:<seq>`. It takes the member out of that primary's
  group, and does nothing if the member has moved elsewhere.
- **`seq`** is a counter per pair of addresses (in either role), shared by links and unlinks, starting
  at one. An entry counts only if its `seq` is above every earlier signed entry for the pair, so re-recording
  an old link cannot undo a later unlink; linking again needs fresh signatures with a higher `seq`. A
  signed entry that breaks a shape rule still uses up its `seq`.
- **Verification** is two checks per signing side: the signature against the published public key, and
  `standard_puzzle_hash(public key) == address` (the identity key never counts). Anyone can redo both
  from the published log.
- **Requirement holdings are summed across the group**, and every address in it is judged by the sum.
  Two Wizards at one address and one at another make 3, enough for a 3-Wizard tier. **Rewards still go
  to the address that holds the LP.**
- **The log, and what a height is bound to.** The operator keeps one append-only log per distributor and
  records each entry at the peak height when it arrives; entries apply in log order from their recorded
  `height` until replaced or unlinked. The height is not signed, so the log binds it: heights never
  decrease along the log, each root manifest commits sha256 of the prefix below its `end` (§6.5), and
  the engine refuses a log that rewrites a committed prefix or adds an entry dated before the last
  root's `end`. A link can therefore never reach back into a window a posted root has settled. Inside
  the open window the recorded height is the operator's word, bounded by the last root's `end` and the
  peak at recording, and visible in the published log (`flpcr.links.log_to_json`, written next to the
  manifest). Link changes cut segments like any other change.
- **Shape:** groups are one level deep (a primary cannot be a member, a primary with members cannot
  become one), with at most 16 members per group. Anything that breaks a rule or fails verification is
  ignored, not an error.
- **What links cannot stop:** two people who both sign can pool holdings, so one Wizard can boost a
  friend's address. That is consent, and it is public.

The interface flow is one button, "link this address": Sage signs with the address holding the LP, then
with the address holding the Wizards, and the API records the link.

---

## 7. Trust model and disputes

| Party | Can | Cannot |
|---|---|---|
| Operator | Post a wrong split | Exceed any campaign's schedule; lower a total; post for the future; skip or repeat a window; make a root claimable before its dispute window ends |
| Guardian | Veto a pending root; resolve a dispute (refund the disputer, or forfeit the bond to the dev multisig and restart the window); propose a new operator key, which takes effect `rotate_delay` after the proposal (production about one day) | Post roots; move funds (a resolved bond goes only to the disputer or the dev multisig); touch the active root; veto a disputed root; make a rotation take effect before its delay (a new proposal restarts the delay) |
| Admin (the dev multisig) | Cancel a pending rotation; resolve a dispute; reclaim a campaign for its creator from `end + claim_window` | Move funds anywhere but to the creator (and a forfeited bond, which only `resolve` sends); reclaim during the window; post or veto roots; rotate the operator |
| Disputer (anyone) | Freeze a pending root by locking a bond coin of `dispute_bond` XCH; get it back if the guardian or admin upholds the dispute | Dispute without a funded bond, or with the wrong amount; dispute an active root; open a second dispute while one is open; touch claims against the active root; recover a forfeited bond |
| Creator | Fund (by their own coin spend); choose the claim window; reclaim leftovers from `end + claim_window` (they, their delegate, or the admin on their behalf; always paid to them); grant and revoke a delegate | Cancel or edit; reclaim during the window; stop a holder claiming before the reclaim lands |
| Router | Authorize which campaigns are created on its distributors (ours: only through our front end, only registry-priced assets) | Touch funds; create a campaign without the creator's own coin spend; claim; reclaim |
| Holder | Claim their own rewards, whenever they like, by key signature or a coin spend; grant and revoke a delegate | Claim anyone else's; choose the recipient or the amount |
| Holder's delegate | Claim the holder's rewards, paid to the holder, until revoked | Grant itself; delegate further; redirect a payout |
| Anyone else | Nothing that moves funds: every payout needs the holder, the creator, a delegate one of them granted, or the admin (paying the creator), and the dev fee is paid at creation | Claim a holder's rewards; reclaim a creator's leftovers |

**Neither the guardian nor the admin can take funds.** The guardian vetoes, resolves disputes and
proposes rotations; the admin cancels rotations, resolves disputes and reclaims for creators, always
paid to the creator. A resolved bond can go only to the disputer (upheld) or to the dev multisig
(rejected), and the bond is the only XCH a CAT distributor ever holds: it sits in its own coin at nonce
3 and never enters the reserve. A compromised guardian
that rotates in a key of its own waits `rotate_delay` in public state: watchers alert, the admin
cancels, and holders can claim everything the active root already pays them in the meantime.

**If the operator disappears**, nothing is stranded: holders keep claiming against the last active
root, and each creator reclaims from `end + claim_window` (and gets back everything any missing root
would have allocated). The only loss is accrual after the last posted root, which returns to the
creator instead of the holders.

**Disputes.** A watcher (`flpcr` in replay mode, run by anyone) recomputes each pending root from
chain data. A root is claimable no sooner than `dispute_blocks` after the block that includes it
(`activates_at = h + 32 + dispute_blocks`). On a mismatch there are two paths inside that window
(production 38 blocks ≈ 12 min, testnet 8):

- **The guardian's bot vetoes.** The operator then posts the corrected root under the same id.
- **Anyone disputes with a bond.** This is Merkl's dispute mechanism with the stake as a coin: there
  anyone freezes a tree by staking 100 EURA and a multisig resolves; here anyone spends a coin of their
  own that creates a bond launcher holding exactly `dispute_bond` of XCH (production 5 XCH, testnet
  0.1 TXCH), the launcher creates the bond coin and announces it, and the `dispute` leaf asserts that
  announcement, so a bond that was never funded, or funded short, cannot open a dispute. The root
  stays pending and cannot activate, however long that takes. The **guardian or admin** then
  `resolve`s: **upheld**, the root is revoked and the bond refunded to the address the disputer named;
  **rejected**, the bond is forfeited to the dev multisig and the dispute window restarts, so a bad
  dispute buys no time. Only one dispute is open at a time, and a root past its window and promoted can
  no longer be disputed. `veto` is refused while a dispute is open. An unresolved dispute freezes
  posting, but holders keep claiming against the previous active root; the guardian already had that
  power by vetoing, so a dispute gives no party anything new over funds.

---

## 8. HTTP API (read side mirrors Merkl v4)

See [`API.md`](API.md). Resources: `GET /v1/opportunities`, `GET /v1/campaigns[/{id}]`,
`GET /v1/users/{address}/rewards` and `GET /v1/roots/{distributor}/{root_id}`. The write side is
`POST /v1/campaigns/quote`, `POST /v1/campaigns/create`, `POST /v1/claims/build`,
`POST /v1/reclaims/build`, `POST /v1/disputes/build` and `POST /v1/disputes/resolve/build`. Addresses
accept `xch1…` or a hex puzzle hash.

---

## 9. Interface

A **🔥 Campaigns** tab in forge-ui, or on whichever site this lands, with four views:

1. **Opportunities.** Every pool and CAT with a live campaign: APR (TWAP-valued), daily emission, time
   left, and a "provide liquidity" deep link into the existing add flow.
2. **Create.** Pick a target (pool search, or a CAT from the Forge registry), the reward asset (any
   registry CAT or XCH; anything else is refused at quote time by `flpcr/router.py`), the deposit, the **campaign
   length** (from 24 min, testnet ~5 min, up to 365 days; dates shown at about 18.75 s per block) and the weighting slider (time ↔ fees, `LP_HOLD`
   only), and the **claim window** after the end (presets 24 h, 7 days, 30 days, or custom). **Gates and boosts:** pick
   an NFT collection (by minter DID, with the collection's name and image shown) or a CAT, a minimum count,
   and whether it gates or boosts (+%); add tiers; set the boost cap. The preview shows what share a holder
   with and without each requirement would get at current holdings. Advanced: min balance and
   the lists. The preview shows the **3% fee**, the resulting budget, per-day emission, an estimated APR
   at current TVL, and the date from which leftovers can be reclaimed. Signing is one Sage request
   (`chip0002_signCoinSpends`): a spend of the creator's own coin that supplies `amount` and sends
   `"k" + campaign_id` to the distributor. The responder adds the router's message and builds the
   `create` spend around it. A bare "give `amount`, request nothing" offer is not enough: `create`
   refuses a campaign whose creator did not spend a coin in the bundle.
3. **My rewards.** Per campaign: earned (engine, not yet posted), claimable (active root − claimed),
   pending (in a dispute window), claimed, and the date from which the creator may reclaim (shown as
   "claim before …"; past it, the button stays live with a warning until the campaign is reclaimed). **Claim** and **Claim all** build the
   bundle: one wallet approval signs every claim leaf of the wallet's addresses (the stand-in spend,
   §5.4), and one coin spend of the wallet pays the network fee; no rewarded address needs a coin.
   Claims that would collide in one spend (§5.4) go in chained generations of the same bundle (the
   sequencer).
4. **My campaigns** (creators). Emitted, claimed, unclaimed, and a **Reclaim** button that unlocks at
   `end + claim_window`.

Spellbook / agent API: `flpcr.quote`, `flpcr.create`, `flpcr.rewards`, `flpcr.claim`, `flpcr.delegate`,
`flpcr.reclaim`, `flpcr.link` and `flpcr.dispute` for users (anyone), `flpcr.engine.run`,
`flpcr.post_root`, `flpcr.watch`, `flpcr.rotate` and `flpcr.resolve` for the operator and guardian, and
`flpcr.cancel_rotation`, `flpcr.resolve` and `flpcr.reclaim` (as admin) for the dev multisig. These sit as tools beside the existing swap quote/build/settle. Inputs,
outputs, who signs and the checks for each are in [`AGENT_TOOLS.md`](AGENT_TOOLS.md).

---

## 10. Phases

| Phase | Deliverable | Gate |
|---|---|---|
| **0 ✅** | Spec; engine (`model`, `engine`, `merkle`, `manifest`, CLI); reference distributor model; router rules | Hashing cross-checked against `clvm` |
| **1: engine on real data** (in progress) | Live `Source` for testnet11 (coinset or full node + `forge_resync`); run against every live V16, V15 and V14 pool; publish manifests. Built: LP holders of every V16 pool (`LiveLpSource`, time share only) and NFT holders (`ChainNftSource` over `NodeReader`); fee events and V15/V14 pools not yet | Replaying a week of testnet from scratch twice gives identical roots |
| **2 ✅ (simulator)** | `contracts/`: 10 leaves plus the bond launcher, claim slots, pins, `build.py`; driver and `SimWorld`; chain, differential and mutation suites, XCH and CAT rewards (§12) | Done on the simulator. Still open: the distributor registry, a `clvmPuzzleAudit.md` pass by a second reader |
| **3: testnet lane** | Router (quote + create, our key), operator bot, watcher, API, forge-ui tab, on `Config.testnet` distributors | One campaign end to end on testnet11: create → roots → claims at several points → reclaim of leftovers |
| **4: hardening** | Guardian as a Forge multisig safe, external review (the public dispute leaf is built; §7) | No mainnet before external review |

---

## 11. Open questions

Decided so far: reward assets (any CAT in a Forge registry pool), campaign length (testnet from ~5 min,
production likely from 24 min, max 365 days), claims at any interval at the holder's discretion, who operates (our router key now, others' later), puzzle
location (this repo, `contracts/`; `forge-puzzles` stays specific to the n-asset AMM).

1. **Minimum reward rate.** Merkl refuses dust campaigns with a per-token minimum per hour. Do we
   want one (checked by our router at quote time, so it needs no puzzle change)?
2. **Registry admission for distributors.** Our router already refuses unpriced assets. Should the
   FLPCR registry *also* refuse to register a distributor whose reward asset is not in a Forge pool,
   which would mean proving a Forge registry slot on chain? Proposal: no. The router check is enough
   while every distributor is ours; revisit when third-party operators arrive.

---

## 12. What the simulator proves, and what it does not

Run everything with `.venv/Scripts/python.exe -m pytest tests -q` (378 tests) after `python contracts/build.py`.

| Suite | Proves |
|---|---|
| `contracts/build.py` | Every puzzle compiles with rue 0.8.4; the five upstream puzzles reproduce slot-machine@2d37ba1's published tree hashes, and their sources match the sha256 pins in `pins.json` (line endings normalized); our ten leaves (`init`, `create`, `post_root`, `veto`, `claim`, `reclaim`, `rotate`, `delegate`, `dispute`, `resolve`, listed once in `flpcr/leaves.py`) and the bond launcher (`EXTRA`) match `pins.json` |
| `tests/test_chain.py` | On the simulator, every chain test class twice: as written for XCH, and as a generated CAT twin. Only the holder or their delegate can claim (`TestOnlyTheHolderClaims`: no authorization, someone else's message and an unknown mode are refused; the holder's key signature claims without a coin at the address, is not replayed, must actually sign, and a key for another address is refused; a claim names no generation, so a message built against one generation lands in a later one); a hot wallet claims for a cold wallet and a vault after a grant, and a delegate claims by key signature too; the sequencer (`TestSequencer`) pays every first claim in one bundle across chained generations, leaves the driver unchanged, sets a bad claim aside, keeps one request per holder and defers what the cost budget does not fit; only the owner grants or revokes, revoked and ungranted delegates are refused, one hop only, grant twice and revoke once is revoked, every grant and revoke pays `delegate_fee`. Creation needs both the router's and the creator's message, and refuses a malformed creator. Reclaim works for the creator, the creator's delegate and the admin, always paid to the creator, and never before the window (a holder's delegate is not the creator's). Rotation waits `rotate_delay`, the old operator posts until then, and the admin cancels. The dispute window counts from `h + 32`, and a campaign cannot start before `h + 32`. Disputes (`TestDisputes`): a disputed root waits past its window; upheld revokes the root and refunds the bond to the disputer, and the corrected root reuses the id; rejected forfeits the bond to the dev multisig and restarts the window; the admin may resolve too; veto waits for resolve; a bond that was never created, or created with the wrong amount, cannot open a dispute; only the pending root can be disputed, once, and never after promotion; only the guardian or admin resolve, with a dispute open and a 0/1 verdict; and the bond coin pays only where the leaf says (a thief's delegated puzzle fails with `MessageNotSentOrReceived`). Batches that collide raise `BundleCollision`. Excluded addresses (`TestExcludedAddresses`): the engine pays neither the burn address nor an excluded one; even from a forged root that pays them, both are refused at claim, and hiding an address by revealing a shorter list fails the stored hash; `create` refuses a list longer than 16 or with a malformed entry. NFT campaigns (`TestNftCampaigns`): NFTs minted on the simulator, one share per NFT, a transfer moves the share, creator exclusion applies, and a look-alike DID earns nothing. Plus the full lifecycle (3% dev fee paid at creation, claims at several points, late claim past the window, leftovers to the creator), the reserve coin equal to state after every step, several actions in one spend, and a refusal probe per rule |
| `tests/test_nfts.py` | The DID evidence rules (§6.8), and `TestCollectionByMetadata`: a `col1` collection counts only NFTs the DID minted whose hash-checked metadata names the collection id; a copied id from another DID, a tampered file, missing metadata and another collection by the same DID are left out |
| `tests/test_lp_crawl.py` | The LP crawl: balances follow the coins, custody is not a position, events before a height, the cache round-trips, and every holder is in the root |
| `tests/test_reclaim_guard.py` | The operator's reclaim guard (§5.5), five cases including the testnet near-miss |
| `tests/test_distributor.py` | The reference model alone, including `test_dispute_and_resolve`: the same dispute rules as `TestDisputes` (wrong root, short bond, one at a time, veto refused, no activation while disputed, a stranger cannot resolve, rejected restarts the window, upheld refunds and the id is reused) and `bonds_paid` records where each released bond went |
| `tests/test_differential.py` | Twelve random scenarios (alternating XCH and CAT) through the reference model and the puzzles side by side, sharing one `Config`: 29 roots, 106 paid claims, 58 refused claims and 13 reclaims (3 of them by the admin); the dev fee paid at creation matches. Every payout and every refusal agrees, and the reserve matches after each step |
| `scripts/mutate.py` | Deleting any single assertion, or any emitted guard line (the height asserts, `receive_from`, `authorize`, `authorize_claim`, `authorize_by_spend`, `AggSigMe`, `ReserveFee`, slot spends), in our leaves makes a test fail, or does not compile (enforced by rue's type narrowing), or is argued in `scripts/mutation-arguments.json` (`amount > 0` and `budget > 0` imply each other while the fee is below 100%). The run must end "0 unargued survivors". The rerun on `claim` and `shared` after the claim-authorization change: 22 killed, 0 unargued survivors, 3 that do not build. After excluded addresses, on `create`, `claim` and `shared`: 44 killed, 0 unargued survivors, 3 that do not build. After `NFT_HOLD`, on `create`: 18 killed, 0 unargued survivors |

A refusal counts only if the leaf raised (`clvm raise`) or consensus rejected the bundle (`Simulator
error`). A Python error in a test or the driver is a bug and fails the test. Every puzzle refusal is
the same `clvm raise`, with no rule named, so a test can only tell which assertion fired by the mutation
run: deleting that assertion must make the test fail. Consensus refusals do name their rule, and the
tests pin those names (for example `AssertHeightAbsoluteFailed`).

**What the first mutation run found.** With every test green, 25 of 37 buildable mutants survived. Three
of the unpinned assertions guard attacks: the slot ordering check (a double claim), `end <= h` in
`post_root` (a root dated in the future releases budget early), and the height pin (a later action naming
a later height reclaims early). The assertions were in the puzzles; the tests that proved them were not.
Each now has a probe built so that the assertion it names is the first thing that can refuse it.

**Not yet proven:**
- **Real consensus.** The simulator runs chia_rs consensus checks, but testnet11 is the gate (phase 3).
- **Cost under load.** The 2026-10-19 review measured XCH costs on the pre-fix build (a claim with 32
  campaigns in state: about 1.6% of a block). CAT costs, many claims in one spend, and the re-pinned
  leaves are not measured yet. `max_campaigns` bounds the state.
- **Real keys.** Router, operator, guardian, admin and the disputer are anyone-can-spend test puzzles
  here. On testnet they are a wallet key (router, operator, disputer), a Forge multisig safe (guardian)
  and the dev multisig (admin).
- **The distributor registry** is not built.
- **No external review.** The pre-testnet review (`reviews/2026-10-19/FINDINGS.md`) is one model pass;
  its on-chain findings are fixed in this build, and its probes have to be rerun against the new pins.
