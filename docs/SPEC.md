# FLPCR — Forge Liquidity Pool Campaign Rewards — Specification

> Incentive campaigns for Forge liquidity and Chia CATs. Anyone can fund a reward budget on a
> Forge pool or a CAT over a block range. Holders earn from it every block and claim whenever
> they like. After the campaign ends and its claim window closes, the creator takes back
> everything holders left unclaimed.
> **FLPCR is the working name.** It appears in identifiers (`flpcr`, `flpcr-*-v1` tags), so a
> rename is a find-and-replace until something is deployed. Merkl is the reference design, not a
> dependency; nothing here uses Merkl's name, contracts or API.
> Status: **draft v0.2, 2026-10-18.** Engine and a reference model of the distributor are built
> and tested; the distributor puzzles are specified but not yet written. Not audited. Testnet first.

**Decisions so far**

| | |
|---|---|
| Creation fee | **3%**, taken out of the deposit, as Merkl does. A 1,000 deposit funds a 970 budget |
| Claims | **Pull, cumulative, at any time**, as Merkl does. Each root lists what a holder has earned so far; a claim pays the difference from what they already claimed |
| Leftovers | After `end + claim_window`, the **creator reclaims** everything not claimed: budget nobody earned plus earnings nobody claimed |

---

## 1. What Merkl does, and what we keep

Merkl ([docs](https://docs.merkl.xyz/merkl-mechanisms/incentive-mechanisms),
[developers](https://developers.merkl.xyz), contracts `AngleProtocol/merkl-contracts`) is
the incentive layer most EVM DEXs use.

| Merkl | How it works there | FLPCR |
|---|---|---|
| `DistributionCreator.createCampaign(CampaignParameters)` | `{creator, rewardToken, amount, campaignType, startTimestamp, duration, campaignData}`; fee deducted from `amount` (3%); min amount per hour per token | `create` leaf on a **distributor singleton**; the campaign struct is hashed into a campaign id; **3% of the deposit** goes to the treasury; campaigns are scheduled by block height |
| Off-chain engine | Every few hours, scores positions (time-weighted liquidity; for CLAMMs, fees/token0/token1 weights, in-range only, boosts, blacklists, forwarders for ALMs) and builds a tree of **cumulative** `(user, token, amount)` | Same model, deterministic and exact (§6). The tree is **cumulative per (campaign, holder)**. It scores LP and CAT holders from chain data, using the Forge replay that already exists |
| `Distributor.updateTree(root)` + dispute | An updater posts a root; it becomes claimable after a dispute period; a bonded disputer can freeze it; governance resolves or revokes | `post_root` leaf, then a **dispute window in blocks** before it becomes active; a guardian can `veto`. The same leaf enforces each campaign's **emission ceiling** on chain (§5.3), which Merkl does not have |
| `Distributor.claim(users, tokens, amounts, proofs)` | Leaf `keccak(user, token, cumulative)`; pays `cumulative − claimed[user][token]`; operators may claim for users | `claim` leaf. Leaf `(campaign, holder, cumulative)`; pays `cumulative − claimed`, with `claimed` kept in a **slot** per (campaign, holder). **Anyone** may submit a claim, but it only ever pays the holder's puzzle hash |
| Unclaimed rewards | Stay claimable forever; governance can reallocate | Claimable until `end + claim_window`; then **the creator reclaims** them (`reclaim` leaf) |
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
| **Claim window** | Blocks after a campaign's `end` during which holders can still claim; after it, the creator reclaims |
| **Operator** | The key allowed to post roots (a coin at `engine_ph` sends a message) |
| **Guardian** | The key (intended to be a Forge multisig safe) that can veto a pending root and rotate the operator |

---

## 3. Campaign kinds

| Kind | `target` | Tracked asset | Fee weighting |
|---|---|---|---|
| `LP_HOLD` (1) | Forge pool launcher id | the pool's LP CAT | yes |
| `CAT_HOLD` (2) | CAT asset id | that CAT | no (time only) |

Phase 2 candidates, each a new `Kind` with its own scoring function and none needing a puzzle change:
`SWAP_VOLUME` (pay traders by routed volume, from the same replay), `VAULT_DEPOSIT`, and `NFT_HOLD`
(collection holders; BOW soulbound tiers would work better as a **boost** than as a kind).

### 3.1 The campaign struct (what the id commits to)

```
campaign = ["flpcr-campaign-v1", distributor_id, creator_ph, reward_asset, amount,
            start, end, kind, target, params_hash, nonce, fee_bps]
campaign_id = sha256tree(campaign)
params = [time_bps, fees_bps, min_balance, sorted(blacklist), sorted(whitelist), forward]
params_hash = sha256tree(params)

fee    = amount * fee_bps / 10000      (floor; fee_bps = 300)
budget = amount − fee                  (what holders can earn)
```

`amount` is the deposit, fee included, as in Merkl's `CampaignParameters.amount`. The rules, checked
by both `model.py` and the `create` leaf, are:
- `amount > 0` and `budget > 0`;
- `end > start` and `start >= h_create` (no retroactive campaigns);
- `end − start <= max_duration`;
- `time_bps + fees_bps = 10000`, and `CAT_HOLD ⇒ fees_bps = 0`;
- `blacklist ∩ whitelist = ∅`;
- `fee_bps` equals the distributor's.

The full `params` value sits in the `create` solution, so it is on chain and recoverable from the hash.

---

## 4. Architecture

```
 creator ──offer──▶ responder ──create──▶ ┌──────────────────────────────┐
                                          │ Distributor singleton        │
 chain ──▶ Engine ──root manifest──▶ IPFS │  action layer, 7 leaves      │
            │                             │  reserve (upstream finalizer)│
            └──post_root (operator)─────▶ │  state: campaigns, roots     │
 guardian ──veto / rotate───────────────▶ │  claim slots (campaign,ph)   │
 holder / anyone ──claim(proofs)────────▶ └──────────┬───────────────────┘
 creator / anyone ──reclaim (after window)───────────▶│ CREATE_COIN, hinted
                                                     ▼
                                          holder's or creator's wallet
```

**One distributor per reward asset.** The distributor is
`singleton(action_layer(reserve_finalizer, leaves_root, state))` with **one** reserve. That is the
upstream CHIP-0050 reserve finalizer **unmodified**. Forge needed a multi-reserve finalizer; this
doesn't, which keeps it out of the audit scope. Every campaign paying in, say, wUSDC goes to the
wUSDC distributor. A registry like Forge's (key = reward asset id, sorted slots, one distributor
per asset) lets wallets and the API find a distributor cold.

---

## 5. The distributor (on chain)

Rue, CHIP-0050 action layer, upstream puzzles hash-pinned exactly as in `contracts/v14/pins.json`.
Leaf-by-leaf detail is in [`PUZZLES.md`](PUZZLES.md). **`flpcr/distributor.py` is the same rules
as running Python**: the reference the puzzles are tested against, and what the engine's output is
pushed through in `tests/test_distributor.py`.

### 5.1 Config (curried, so the leaf root commits to it) and state

```
Config  = { distributor_id, reward_asset, treasury_ph, fee_bps = 300, guardian_ph,
            dispute_blocks, claim_window, max_campaigns, max_duration }
State   = { engine_ph, campaigns: List<Campaign>, active: Root?, pending: Root?,
            fees_owed, reserve_total }
Campaign (in state) = (campaign_id, creator_ph, budget, start, end, claimed)
Root     = (root_id, start, end, root, totals: ((campaign_id . cumulative_total) ...), activates_at)
Claim slot (one per campaign and holder) = ((campaign_id . puzzle_hash) . claimed)
```

`reserve_total = Σ (budget − claimed) over campaigns + fees_owed`. The reserve finalizer sizes
the reserve coin from it. Conservation is the reserve's job, and honesty about this sum is each
leaf's job (the Forge rule, `chip0050ActionLayer.md`).

### 5.2 Leaves (fixed forever per distributor)

| Leaf | Who | Does |
|---|---|---|
| `create` | anyone | Binds a settlement of at least `amount` (V14 `settlement_binding`: parent + amount + `ASSERT_CONCURRENT_SPEND`); validates the struct; appends the campaign with `claimed = 0`; accrues `fee` + any slack to `fees_owed`. Refuses a duplicate id or a full list |
| `post_root` | operator (mode-23 message from a coin at `engine_ph`) | Sets `pending`. Checks §5.3. `activates_at = h + dispute_blocks` |
| `veto` | guardian (mode-23 message from `guardian_ph`) | Clears `pending` while it is still in its dispute window |
| `claim` | anyone | Against the **active** root and within the campaign's claim window: verifies `(campaign, ph, cumulative)`, pays `cumulative − claimed` from the reserve to `ph` (hinted), and records `cumulative` in the claim slot |
| `reclaim` | anyone, after `end + claim_window` | Pays `budget − claimed` to `creator_ph` (hinted) and removes the campaign |
| `collect` | anyone | Pays `fees_owed` to `treasury_ph` |
| `rotate` | guardian | Replaces `engine_ph` |

There is no upgrade leaf and no "withdraw everything" leaf. A compromised operator and a
compromised guardian together can still pay out only up to each campaign's emission schedule
(§5.3), and only after a dispute window.

**Pending becomes active lazily.** The prologue of whichever spend comes next promotes `pending`
to `active` once `h >= activates_at`. A new root cannot be posted while one is still in its
dispute window, which is Merkl's `updateTree` rule.

### 5.3 The emission ceiling (the on-chain guarantee)

`post_root`'s solution carries `(root_id, start, end, root, totals, manifest_hash)`, where
`totals[c]` is the sum of campaign `c`'s cumulatives in this tree. The leaf asserts:

```
root_id == last.root_id + 1     start == last.end (any for the first)     start < end <= h
for each live campaign c in the last root's totals:  c is in totals                (never dropped)
for each (c, t) in totals:  c is live;   t >= last.totals[c];   t <= budget_c · (min(end, end_c) − start_c) / (end_c − start_c)
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
exists for its key. FLPCR does this the same way the Forge registry does, with **sorted slots and
sentinels**: inserting `(c, ph)` spends its left neighbour, proving adjacency, and recreates it
pointing at the new slot. A later claim spends only the holder's own slot. Claims serialize on the
singleton anyway (each one pays from the reserve), so neighbour contention costs nothing extra.
Several claims, for several holders and campaigns, batch into one singleton spend.

**Wallet experience.** The interface (and any keeper) builds the claim bundle and the holder signs
nothing but the network fee, through a Sage offer that pays it. Because the payout always goes to the
holder's puzzle hash, a third party can claim *for* a holder (Merkl's operator role) without being
able to redirect anything. A holder's rewards can therefore be auto-claimed by a keeper if they opt
in.

### 5.5 Leftovers go back to the creator

`claim_window` is distributor config (default 30 days = 138,240 blocks). For each campaign:

- `claim` is refused at `h >= end + claim_window`;
- `reclaim` is refused before then; after it, it pays `budget − claimed` to the creator and removes
  the campaign (its claim slots stay as inert 0-mojo coins).

`budget − claimed` covers both kinds of leftover: budget **nobody earned** (stretches with no eligible
holder, floor dust) and earnings **nobody claimed**. The operator's duty is to post the root covering
`end` early enough that it activates well inside the window. A watcher alerts if `end + dispute_blocks`
passes with no such root.

---

## 6. The engine (off chain): normative scoring

Implemented in `flpcr/engine.py`. A second implementation must produce byte-identical roots. All
arithmetic is exact rational; the only rounding is a floor of a holder's cumulative accrual.

### 6.1 Inputs, all from chain

- **Holder timeline** of the tracked asset: every CAT coin creation (+amount) and spend (−amount),
  attributed to its inner puzzle hash via the CHIP-0020 hint (Forge LP coins are hinted). A change at
  height `h` is in force on `[h, …)`.
- **Exclusions:** pool reserve puzzle hashes, OFFER_MOD settlement coins (transient, created and spent
  within a route), the zero hash (the `LOCKED_BURN` floor) and the protocol treasury. They are derived
  from the Forge registry, never configured by hand.
- **Fee events** for `LP_HOLD`: each pool generation is replayed with `forge_v14_resync`. Resolve each
  action's leaf through its selector, not `zip(puzzles, solutions)` (`forgePuzzleV14.md` §5). Each
  `swap` action yields `(h, weight)`, where weight is the LP fee it paid, valued in XCH at the pool's
  **pre-spend** spot price (the price the oracle accumulates in the prologue).
- **Forwarders:** Forge vault pools whose reserves include this pool's LP, mapped to the vault's own LP
  asset.

### 6.2 Time weighting (variable rate)

Take the root's window `[a, b)` clipped to the campaign and cut it at every balance change into segments
`[s, t)`. With `E(h) = campaign.emittable(h)` and eligible balances `B_u`:

```
budget(s,t) = (E(t) − E(s)) · time_bps / 10000
credit_u   += budget(s,t) · B_u / Σ B
```

Each block's emission is split by share of eligible supply, the same as Merkl's variable rate and
MasterChef. A segment with no eligible holder credits no one, and its budget comes back to the creator
at reclaim.

### 6.3 Fee weighting (`LP_HOLD` only)

```
fee_budget  = (E(b) − E(a)) · fees_bps / 10000
credit_u   += fee_budget · Σ_e w_e · B_u(h_e)/ΣB(h_e)  /  Σ_e w_e
```

This runs over fee events `e` in the window that have at least one eligible holder. If the window has
**no** such event, the fee share is spent on time weighting, so a quiet pool doesn't strand the budget.
This is the fungible-LP form of Merkl's `propFees`: an LP's fee share in a block *is* their LP share, so
weighting by fees moves rewards toward the blocks where the liquidity was used. Because the fallback is
decided per window, root boundaries are part of the result. They are posted on chain (`start`, `end`),
so a replay uses the same ones.

### 6.4 Forwarding

A wrapper's balance `W` at height `h` is credited to its share holders `p` as `W · S_p / ΣS`, with the
share asset's own exclusions applied. Forwarding is one level deep: a wrapper of a wrapper is credited
to the inner wrapper's puzzle hash. A campaign disables it with `forward = 0`.

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
`(path, hashes)`), which `simplify_merkle_proof` verifies on chain. A window in which nothing accrued is
not posted, and the next run covers the longer window, so posted roots tile the timeline. A campaign
that has been reclaimed is left out of the engine's input and drops out of later trees.

### 6.6 Valuation (display only in v1)

APR and TVL in the API use the pools' oracle accumulators (`observe` snapshots and the cumulative price
in state) for a TWAP, not a spot price that can be moved. In v1 no reward depends on a price except fee
weights (§6.3), which use the pre-spend spot of the pool that paid them. Fixed-rate / capped-APR
campaigns would make price load-bearing, and must use the TWAP only.

### 6.7 Anti-gaming

Time weighting per block makes a flash deposit worth one block's share. The minimum balance and the
blacklist apply. Self-trading to farm fee weight costs the trader the full pool fee for a share of a
**fixed** budget, so it pays only when the fee weight per mojo of fee is worth more than the fee. The
create form should show creators that trade-off next to the `fees_bps` slider.

---

## 7. Trust model and disputes

| Party | Can | Cannot |
|---|---|---|
| Operator | Post a wrong split | Exceed any campaign's schedule; lower a total; post for the future; skip or repeat a window; make a root claimable before its dispute window ends |
| Guardian | Veto a pending root; rotate the operator | Post roots; move funds; touch the active root |
| Creator | Fund; reclaim leftovers after `end + claim_window` | Cancel or edit; reclaim during the window |
| Holder / keeper | Claim (for themselves or anyone) | Choose the recipient or the amount |

**Disputes, v1:** a watcher (`flpcr` in replay mode, run by anyone) recomputes each pending root from
chain data. On a mismatch it alerts the guardian, which vetoes within `dispute_blocks` (default
1,152 ≈ 6 h); the operator then posts the corrected root under the same id. **v2:** a public `dispute`
leaf. Anyone posts a bond in the reward asset, which freezes activation until the guardian resolves it.
The bond comes back on a valid dispute and is kept otherwise, like Merkl's `Disputer`.

---

## 8. HTTP API (read side mirrors Merkl v4)

See [`API.md`](API.md). Resources: `GET /v1/opportunities`, `GET /v1/campaigns[/{id}]`,
`GET /v1/users/{address}/rewards` and `GET /v1/roots/{distributor}/{root_id}`. The write side is
`POST /v1/campaigns/quote`, `POST /v1/campaigns/create`, `POST /v1/claims/build` and
`POST /v1/reclaims/build`. Addresses accept `xch1…` or a hex puzzle hash.

---

## 9. Interface

A **🔥 Campaigns** tab in forge-ui, or on whichever site this lands, with four views:

1. **Opportunities.** Every pool and CAT with a live campaign: APR (TWAP-valued), daily emission, time
   left, and a "provide liquidity" deep link into the existing add flow.
2. **Create.** Pick a target (pool search or CAT asset id), the reward asset, the deposit, the duration
   (with the date shown, at about 18.75 s per block) and the weighting slider (time ↔ fees, `LP_HOLD`
   only). Advanced: min balance and the lists. The preview shows the **3% fee**, the resulting budget,
   per-day emission, an estimated APR at current TVL, and the claim-window date after which leftovers
   return. Signing is one Sage offer: give `amount`, request nothing. The responder builds the `create`
   spend around it, the same keyless pattern as Forge's offer lane (`forge_v14_offer.py`).
3. **My rewards.** Per campaign: earned (engine, not yet posted), claimable (active root − claimed),
   pending (in a dispute window), claimed, and the claim deadline. **Claim** and **Claim all** build one
   bundle; the holder's offer pays the network fee.
4. **My campaigns** (creators). Emitted, claimed, unclaimed, and a **Reclaim** button that unlocks at
   `end + claim_window`.

Spellbook / agent API: `flpcr.quote`, `flpcr.create`, `flpcr.rewards`, `flpcr.claim` and
`flpcr.reclaim`, as tools beside the existing swap quote/build/settle.

---

## 10. Phases

| Phase | Deliverable | Gate |
|---|---|---|
| **0 (this repo)** | Spec; engine (`model`, `engine`, `merkle`, `manifest`, CLI); reference distributor model; tests | 35 tests green; hashing cross-checked against `clvm` |
| **1: engine on real data** | Live `Source` for testnet11 (coinset or full node + `forge_v14_resync`); run against the 32 V14 pools; publish manifests | Replaying a week of testnet from scratch twice gives identical roots |
| **2: distributor puzzles** | `contracts/flpcr/` rue project: 7 leaves, claim slots, registry, pins; driver; suites in the V14 style (integrity, before/after, mutation with UNREACHED reporting) that also run every scenario in `tests/test_distributor.py` against the puzzles | `clvmPuzzleAudit.md` pass; every refusal in the reference model reproduced by a puzzle refusal |
| **3: testnet lane** | Operator bot, watcher, API, forge-ui tab | One campaign end to end on testnet11: create → roots → claims at several points → reclaim of leftovers |
| **4: hardening** | Public dispute leaf, guardian as a Forge multisig safe, external review | No mainnet before external review |

---

## 11. Open questions

1. **Reward assets allowed.** Any CAT, or an allowlist? Merkl keeps a min-amount-per-hour list per
   token. Proposal: any CAT, plus a minimum rate per distributor fixed at launch.
2. **Claim window length.** 30 days proposed. Per distributor (one rule for every campaign, simpler
   to explain) or chosen per campaign by the creator within bounds?
3. **Operator decentralization.** One operator key in v1. Later: M-of-N operator messages, or several
   engines that must post the same root.
4. **Puzzle location.** In `forge-puzzles` (one audit surface) or here under `contracts/`.
