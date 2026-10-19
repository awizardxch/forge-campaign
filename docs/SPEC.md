# FLPCR — Forge Liquidity Pool Campaign Rewards — Specification

> Incentive campaigns for Forge liquidity and Chia CATs. Anyone can fund a reward budget on a
> Forge pool or a CAT over a block range. Holders earn from it every block and claim whenever
> they like. Once the claim window the creator chose has passed, the creator may take back
> everything holders left unclaimed; until they actually do, holders can still claim.
> **FLPCR is the working name.** It appears in identifiers (`flpcr`, `flpcr-*-v1` tags), so a
> rename is a find-and-replace until something is deployed. Merkl is the reference design, not a
> dependency; nothing here uses Merkl's name, contracts or API.
> Status: **v0.3, 2026-10-18.** Engine, reference model **and the distributor puzzles** are built.
> The puzzles run on the in-process simulator (chia-wallet-sdk 0.36.0) for XCH and CAT rewards, agree
> with the reference model on randomized scenarios, and every leaf assertion is pinned by a probe
> (§12). Not on testnet yet. Not audited.

**Decisions so far**

| | |
|---|---|
| Creation fee | **3%**, taken out of the deposit, as Merkl does. A 1,000 deposit funds a 970 budget |
| Claims | **Pull, cumulative, at any time**, as Merkl does. Each root lists what a holder has earned so far; a claim pays the difference from what they already claimed. **Only the holder can claim**, by their own signature or a message from their own address, **or a delegate the holder granted** (a hot wallet claiming for a cold wallet; rewards still go to the cold wallet) (§5.4) |
| Claim window | **Set by the creator at creation** and committed in the campaign id, so the terms are complete up front. It opens reclaiming; it does not close claiming |
| Leftovers | From `end + claim_window`, the **creator may reclaim** everything not claimed: budget nobody earned plus earnings nobody claimed. **Holders can claim until the reclaim lands**: a 24 h window with no reclaim after 48 h still pays a late claimer |
| Campaign length | Per distributor. **Testnet: from ~5 min** (16 blocks, 8-block disputes). **Production: likely from 24 min** (77 blocks, 38-block ≈ 12 min disputes). Maximum **365 days**. Any of these can change for a future distributor |
| Claim frequency | **Any interval within the campaign**, at the holder's discretion: every root, once at the end, or anything between. Each claim costs the holder a network fee, so how often is their call. A new root becomes claimable every `dispute_blocks` at most |
| Assets | **Any CAT that trades in a Forge registry pool**, plus XCH, for the reward asset and for a `CAT_HOLD` target. The pool's oracle gives every such asset an on-chain price. An `LP_HOLD` target must be a registered pool |
| Gates and boosts | A campaign can **require** holdings to earn at all (at least N NFTs of a collection, e.g. the Wizards, or at least X of a CAT) and **boost** holders who meet further requirements (+N% weight per tier, up to a cap the creator sets). Engine-side rules, committed in the campaign id; no puzzle change (§6.8) |
| Who runs it | The puzzle is on chain and permissionless to read and claim from. **Creation needs the distributor's router key**: ours now, so we only score and monitor campaigns our router created. Later, anyone can launch their own distributor with their own router, engine and front end |

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
| `Distributor.claim(users, tokens, amounts, proofs)` | Leaf `keccak(user, token, cumulative)`; pays `cumulative − claimed[user][token]`; operators may claim for users | `claim` leaf. Leaf `(campaign, holder, cumulative)`; pays `cumulative − claimed`, with `claimed` kept in a **slot** per (campaign, holder). **Only the holder can claim** (signature or message from their address); there is no operator claiming on a holder's behalf |
| Unclaimed rewards | Stay claimable forever; governance can reallocate | Claimable until the creator reclaims, which they may do from `end + claim_window` (`reclaim` leaf) |
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
| **Guardian** | The key (intended to be a Forge multisig safe) that can veto a pending root and rotate the operator |

---

## 3. Campaign kinds

| Kind | `target` | Tracked asset | Fee weighting |
|---|---|---|---|
| `LP_HOLD` (1) | Forge pool launcher id | the pool's LP CAT | yes |
| `CAT_HOLD` (2) | CAT asset id | that CAT | no (time only) |

Phase 2 candidates, each a new `Kind` with its own scoring function and none needing a puzzle change:
`SWAP_VOLUME` (pay traders by routed volume, from the same replay), `VAULT_DEPOSIT`, and `NFT_HOLD`
(rewarding collection holders directly). Gating and boosting by NFT or CAT holdings is not a kind: it
applies to every kind (§6.8).

### 3.1 The campaign struct (what the id commits to)

```
campaign = ["flpcr-campaign-v1", distributor_id, creator_ph, reward_asset, amount,
            start, end, kind, target, params_hash, nonce, fee_bps, claim_window]
campaign_id = sha256tree(campaign)
params = [time_bps, fees_bps, min_balance, sorted(blacklist), sorted(whitelist), forward,
          gates, boosts, boost_cap_bps]
          gates  = ([holding, asset, min_count] ...)            holding: 1 = NFT, 2 = CAT
          boosts = ([holding, asset, min_count, bps] ...)
params_hash = sha256tree(params)

fee    = amount * fee_bps / 10000      (floor; fee_bps = 300)
budget = amount − fee                  (what holders can earn)
```

`amount` is the deposit, fee included, as in Merkl's `CampaignParameters.amount`. The rules, checked
by both `model.py` and the `create` leaf, are:
- `amount > 0` and `budget > 0`;
- `end > start` and `start >= h_create` (no retroactive campaigns);
- `min_campaign_length <= end − start <= max_campaign_length` (testnet from 16 blocks ≈ 5 min, production
  from 77 ≈ 24 min, up to 365 days);
- `time_bps + fees_bps = 10000`, and `CAT_HOLD ⇒ fees_bps = 0`;
- `blacklist ∩ whitelist = ∅`;
- `fee_bps` equals the distributor's;
- `2 · dispute_blocks <= claim_window <= max_campaign_length`. The floor is technical only (§5.5); the creator
  otherwise picks freely.

The full `params` value sits in the `create` solution, so it is on chain and recoverable from the hash.

---

## 4. Architecture

```
 creator ──offer──▶ responder ──create──▶ ┌──────────────────────────────┐
                                          │ Distributor singleton        │
 chain ──▶ Engine ──root manifest──▶ IPFS │  action layer, 9 leaves      │
            │                             │  reserve (upstream finalizer)│
            └──post_root (operator)─────▶ │  state: campaigns, roots     │
 guardian ──veto / rotate───────────────▶ │  claim slots (campaign,ph)   │
 holder / anyone ──claim(proofs)────────▶ └──────────┬───────────────────┘
 creator / their delegate ──reclaim (after window)──▶│ CREATE_COIN, hinted
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
curried `router_ph`, whose content is the campaign id. So a distributor only ever holds campaigns
its operator has agreed to score, and nobody can fund a campaign that no engine watches. Today there
is one operator (us): our router, engine and front end. Running a second operator later means
launching another set of distributors with a different `router_ph`, `engine_ph` and `guardian_ph`,
with no change to the puzzles. Claims and reclaims never involve the router; they work even if the
operator disappears (§7).

---

## 5. The distributor (on chain)

Rue, CHIP-0050 action layer, upstream puzzles hash-pinned exactly as in `contracts/v14/pins.json`.
Leaf-by-leaf detail is in [`PUZZLES.md`](PUZZLES.md). **`flpcr/distributor.py` is the same rules
as running Python**: the reference the puzzles are tested against, and what the engine's output is
pushed through in `tests/test_distributor.py`.

### 5.1 Config (curried, so the leaf root commits to it) and state

```
Config  = [ distributor_id, reward_asset, fee_bps = 300, treasury_ph, guardian_ph, router_ph,
            dispute_blocks, min_campaign_length, max_campaign_length, max_campaigns, slot_1st_curry_hash ]
State   = [ reserve, fees_owed, engine_ph, initialized, campaigns, active, pending ]
Campaign (in state) = [ id, creator_ph, budget, start, end, claim_window, claimed ]
Root     = [ root_id, start, end, root, totals: ((campaign_id . cumulative_total) ...), activates_at ]
Claim slot = (key . (claimed . (left . right))),   key = sha256tree((campaign_id . puzzle_hash))
```

These are the exact layouts in `contracts/puzzles/flpcr/shared.rue` (proper lists; the slot is dotted).
`reserve` is the first state field, so the upstream reserve finalizer's default amount program `(f State)`
sizes the reserve coin and no custom program is needed. The invariant is
`reserve = Σ (budget − claimed) over campaigns + fees_owed`. Conservation is the reserve's job, and honesty about this sum is each
leaf's job (the Forge rule, `chip0050ActionLayer.md`).

### 5.2 Leaves (fixed forever per distributor)

| Leaf | Who | Does |
|---|---|---|
| `init` | anyone, once | Creates the two sentinel claim slots (keys `0x00…` and `0xff…`); `initialized` makes it one-shot, because a second pair would let one key be inserted twice |
| `create` | router (mode-23 message `"c" + campaign_id` from a coin at `router_ph`) | Validates the struct and derives the id; appends the campaign with `claimed = 0`; `reserve += amount`, `fees_owed += fee`. The deposit is enforced by **conservation**, not by naming a coin: the reserve is recreated at the new size, so the bundle (for a CAT, the reserve's CAT ring) must supply `amount` or it is invalid. Who supplies it is not the puzzle's concern; any excess is ordinary bundle slack (network fee) |
| `post_root` | operator (mode-23 message `"p" + sha256tree([root_id, start, end, root, totals, manifest_hash])` from `engine_ph`) | Sets `pending`. Checks §5.3. `activates_at = h + dispute_blocks` |
| `veto` | guardian (mode-23 message `"v" + sha256tree(root_id)` from `guardian_ph`) | Clears `pending` while it is still in its dispute window |
| `claim` | **the holder** (`AGG_SIG_ME` from the key of `ph`, or a mode-23 message `"k" + leaf` from a coin at `ph`), **or a delegate the holder granted** (same two forms, from the delegate's key or address, plus the holder's delegation slot) | Against the **active** root, for any campaign not yet reclaimed (no deadline): verifies `(campaign, ph, cumulative)`, pays `cumulative − claimed` from the reserve to `ph` (hinted), and records `cumulative` in the claim slot |
| `reclaim` | **the creator only** (signature or message over `sha256tree(["flpcr-reclaim-v1", distributor_id, campaign_id])`, or a delegate the creator granted), from the campaign's `end + claim_window` | Pays `budget − claimed` to `creator_ph` (hinted) and removes the campaign |
| `collect` | anyone | Pays `fees_owed` to `treasury_ph` |
| `rotate` | guardian (message `"r" + new_engine_ph`) | Replaces `engine_ph` |
| `delegate` | the holder (signature or message over `sha256tree(["flpcr-delegate-v1", distributor_id, holder, delegate, grant])`) | `grant = 1` creates a delegation slot `(holder . delegate)`, hinted with the holder; `grant = 0` spends it. State is unchanged |

Every message is mode 23: sender identified by puzzle hash, receiver by **coin id**, the current
singleton coin. A message is therefore bound to one generation of the distributor and cannot be
replayed against a later one.

**The height pin.** Every solution starts with `h`. The first action of a spend asserts
`ASSERT_HEIGHT_ABSOLUTE h` and `ASSERT_BEFORE_HEIGHT_ABSOLUTE h + 32` and stores `h` in ephemeral
state; every later action in the same spend must name the same `h`. Without that last rule a second
action could claim a later height than the first, for example to reclaim before the window opens.
The mutation run found the test that proves it was missing (§12).

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
of them touch the same slot.

**Only the holder can claim their own rewards.** A claim must carry the holder's authorization, in
one of two forms, and the puzzle refuses it otherwise:

| `auth` | Who it suits | What the puzzle checks |
|---|---|---|
| `0` signature | every standard wallet address (Sage's normal addresses) | `auth_data` is a synthetic public key with `standard_puzzle_hash(key) == ph` (the puzzle recomputes the curried standard puzzle hash), and the spend carries `AGG_SIG_ME(key, leaf)`. `AGG_SIG_ME` binds the signature to this singleton coin and the network, so it cannot be replayed on a later generation |
| `1` message | an address that is not a standard wallet: a vault, a Forge multisig safe, any other puzzle | `RECEIVE_MESSAGE` mode 23 of `"k" + leaf` from a coin whose puzzle hash is `ph`. Only whoever controls that puzzle can make that coin spend |

The authorized message is the claim leaf itself, `sha256tree(["flpcr-claim-v1", distributor_id,
campaign_id, ph, cumulative])`, so an authorization names exactly one claim. The payout always goes to
`ph`, so even a wrong authorization could never redirect funds. The rule is about who decides when
rewards move.

**A delegate the holder chose.** A holder can let one specific other address claim for them: a cold
wallet whose hot wallet (or a cloud wallet we run for them) approves claims, or a vault whose signer
does. This is Merkl's operator role, except the holder picks the operator, and there is no
protocol-wide one.

1. **Grant, once.** The holder authorizes `delegate` with grant 1, in either form (§5.2). A vault or
   multisig grants by message from its own address and never has to sign a claim again. The
   distributor creates a **delegation slot** `(holder . delegate)`: its own slot type (nonce 2), so it
   can never be read as a claim slot.
2. **Claim, any number of times.** `auth = 2`, `auth_data = (delegate . (mode . data))`. The claim
   spends and recreates the holder's delegation slot for that delegate (proving the grant exists), and
   the delegate authorizes the leaf by its own signature (mode 0) or message (mode 1). The payout goes
   to the holder.
3. **Revoke.** The holder authorizes grant 0; the slot is spent, and the delegate's next claim finds no
   slot and is refused. The holder can always still claim directly.

The rules the tests pin:
- **Only the holder grants or revokes:** a delegate cannot grant itself, and a stranger's message
  cannot grant for an address.
- **Only the granted delegate claims:** another key, even with a valid signature of its own, is
  refused.
- **One hop:** a delegate cannot delegate.
- **Grant is exactly 0 or 1.**
- **Rewards go to the holder:** the delegate's balance never changes. A grant made once serves every
  later claim, across roots.

**Wallet experience.** The interface builds the claim bundle; the holder approves it in Sage. For a
standard address that is one signing request (`chip0002_signCoinSpends`, which signs the `AGG_SIG_ME`
for the address's key) plus the network fee. The fee can come from any of the holder's coins, at any
address, because only the authorization has to come from the rewarded address.

### 5.5 Leftovers go back to the creator

The **creator sets `claim_window` when creating the campaign**. It is part of the struct and the id, so
anyone reading the campaign sees every term: budget, schedule, scoring, and how long holders are
guaranteed to have after the end. For each campaign:

- `reclaim` is refused before `end + claim_window`. From then on **only the creator** can submit it,
  in the same three forms a holder uses to claim: their signature, a message from their address (a vault
  or multisig creator), or a delegate they granted with the same `delegate` leaf (a hot wallet for a
  cold-wallet creator; a holder's delegate is not the creator's). It pays
  `budget − claimed` to the creator and removes the campaign (its claim slots stay as inert 0-mojo
  coins).
- `claim` has **no deadline**. It works for as long as the campaign exists, so a holder who comes back
  after the window, before the creator has reclaimed, is still paid in full. The window guarantees
  holders a minimum; it never takes anything from them early.
- A claim and a reclaim racing in the same block are serialized by the singleton: whichever spend
  lands first wins, and the other is rebuilt against the new state (the late claim then finds the
  campaign gone; the late reclaim pays out less).

The claim window's only floor is technical: at least twice `dispute_blocks`, so the root covering `end`
can be posted and clear its dispute window before reclaiming opens. Its ceiling is the longest campaign,
365 days.

### 5.6 Campaign length and claim cadence

The distributor bounds **campaign length** (`end − start`):

| Preset (`Config` in `distributor.py`) | `dispute_blocks` | `min_campaign_length` | `max_campaign_length` |
|---|---|---|---|
| `Config.testnet(...)` | 8 (≈ 2.5 min) | 16 (≈ 5 min) | 365 days |
| `Config(...)`, production | 38 (≈ 12 min) | 77 (≈ 24 min) | 365 days |

Within a campaign, holders claim **at any interval they choose**. The engine posts a root as soon as the
previous one is active, so a fresh root becomes claimable about every `dispute_blocks`, and a holder can
claim after each one, once at the end, or anything between. Each claim costs the holder a network fee;
whether frequent small claims are worth it is up to them. The interface shows the claimable amount next
to the estimated fee.

A 24-minute campaign is short, and the operator's cadence has to fit it. The engine must post the root
covering `end` within a few blocks of `end`, and the guardian's watcher must veto within the dispute
window. Both are bots, not people: a 12-minute dispute window is too short for a human guardian to react
to, and the automated watcher is what makes it safe.

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
the DID's owner can mint into it. So a campaign's NFT `asset` is the minter DID's launcher id.

*Built: `flpcr/chain/nfts.py`, tested on the simulator (`tests/test_nfts.py`).* A collection is found by
walking **the DID's own spends** and taking every NFT launcher a DID spend created (directly, or through
an intermediate launcher). Nothing but the DID's owner can make a DID coin spend, so the walk cannot be
fooled. The reader deliberately ignores two weaker signals:
- **The metadata collection id**, which can be copied.
- **The NFT's owner-DID field.** An NFT minted elsewhere can later be transferred to the DID, so the
  field does not prove minting.

Each NFT is then followed spend by spend. Its owner is the p2 puzzle hash, which is also its hint, and
each change of owner becomes `−1` for the old owner and `+1` for the new, like a CAT balance. NFTs sitting
in an offer's settlement puzzle are nobody's holding. The tests mint Wizards from one DID and a look-alike
from another, transfer one, and check that the gate counts exactly the Wizards, at the right owners, from
the right height. Two alternatives, for when that does not fit:
- **A fixed list.** For a collection minted without a DID, or a curated subset (e.g. only certain
  Wizards), `asset` is the root of a published list of NFT launcher ids. The list is part of the
  campaign's published terms. *(Not built; the engine's source interface takes it without change.)*
- **Soulbound NFTs** (BOW rewards, `nftRewards.md`) work as is: they cannot be transferred, so the gate
  cannot be lent at all.

**One wallet, many addresses: address links.** *Built: `flpcr/links.py`, tested in `tests/test_links.py`
with real BLS keys.* Sage spreads a wallet across many addresses (puzzle hashes). A holder whose Wizard
sits at one address and LP at another would not match, because requirements are checked per address.
A **link** joins addresses into one holder for gates and boosts only:

- **Both addresses sign** the same message, `flpcr-link-v1:<primary>:<member>`, in Chia's
  `signMessageByAddress` format (CHIP-0002, UTF-8: the synthetic key signs
  `sha256tree(("Chia Signed Message", message))`). A one-sided link would let anyone attach their LP to
  someone else's Wizard, so the primary must consent too.
- **Verification** is two checks per side: the signature against the published public key, and
  `standard_puzzle_hash(public key) == address`. Anyone can redo both from the published link.
- **Requirement holdings are summed across the group**, and every address in it is judged by the sum.
  Two Wizards at one address and one at another make 3, enough for a 3-Wizard tier. **Rewards still go
  to the address that holds the LP.**
- **Over time:** a link counts from the height the operator recorded it (published with the manifests)
  until it is replaced. Linking an address again moves it; an unlink (member == primary, signed by that
  address alone) takes it out of its group. Link changes cut segments like any other change.
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
| Guardian | Veto a pending root; rotate the operator | Post roots; move funds; touch the active root |
| Creator | Fund; choose the claim window; reclaim leftovers from `end + claim_window` (only they, or their delegate); grant and revoke a delegate | Cancel or edit; reclaim during the window; stop a holder claiming before the reclaim lands |
| Router | Authorize which campaigns are created on its distributors (ours: only through our front end, only registry-priced assets) | Touch funds; claim; reclaim |
| Holder | Claim their own rewards, whenever they like; grant and revoke a delegate | Claim anyone else's; choose the recipient or the amount |
| Holder's delegate | Claim the holder's rewards, paid to the holder, until revoked | Grant itself; delegate further; redirect a payout |
| Anyone else | Submit a fee collection (it always pays the treasury) | Claim a holder's rewards; reclaim a creator's leftovers |

**If the operator disappears**, nothing is stranded: holders keep claiming against the last active
root, and each creator reclaims from `end + claim_window` (and gets back everything any missing root
would have allocated). The only loss is accrual after the last posted root, which returns to the
creator instead of the holders.

**Disputes, v1:** a watcher (`flpcr` in replay mode, run by anyone) recomputes each pending root from
chain data. On a mismatch the guardian's bot vetoes within `dispute_blocks` (production 38 blocks ≈
12 min, testnet 8); the operator then posts the corrected root under the same id. **v2:** a public `dispute`
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
2. **Create.** Pick a target (pool search, or a CAT from the Forge registry), the reward asset (any
   registry CAT or XCH; anything else is refused at quote time by `flpcr/router.py`), the deposit, the **campaign
   length** (from 24 min, testnet ~5 min, up to 365 days; dates shown at about 18.75 s per block) and the weighting slider (time ↔ fees, `LP_HOLD`
   only), and the **claim window** after the end (presets 24 h, 7 days, 30 days, or custom). **Gates and boosts:** pick
   an NFT collection (by minter DID, with the collection's name and image shown) or a CAT, a minimum count,
   and whether it gates or boosts (+%); add tiers; set the boost cap. The preview shows what share a holder
   with and without each requirement would get at current holdings. Advanced: min balance and
   the lists. The preview shows the **3% fee**, the resulting budget, per-day emission, an estimated APR
   at current TVL, and the date from which leftovers can be reclaimed. Signing is one Sage offer: give `amount`, request nothing. The responder builds the `create`
   spend around it, the same keyless pattern as Forge's offer lane (`forge_v14_offer.py`).
3. **My rewards.** Per campaign: earned (engine, not yet posted), claimable (active root − claimed),
   pending (in a dispute window), claimed, and the date from which the creator may reclaim (shown as
   "claim before …"; past it, the button stays live with a warning until the campaign is reclaimed). **Claim** and **Claim all** build one
   bundle; the holder's offer pays the network fee.
4. **My campaigns** (creators). Emitted, claimed, unclaimed, and a **Reclaim** button that unlocks at
   `end + claim_window`.

Spellbook / agent API: `flpcr.quote`, `flpcr.create`, `flpcr.rewards`, `flpcr.claim` and
`flpcr.reclaim`, as tools beside the existing swap quote/build/settle.

---

## 10. Phases

| Phase | Deliverable | Gate |
|---|---|---|
| **0 ✅** | Spec; engine (`model`, `engine`, `merkle`, `manifest`, CLI); reference distributor model; router rules | Hashing cross-checked against `clvm` |
| **1: engine on real data** (next) | Live `Source` for testnet11 (coinset or full node + `forge_v14_resync`); run against the 32 V14 pools; publish manifests | Replaying a week of testnet from scratch twice gives identical roots |
| **2 ✅ (simulator)** | `contracts/`: 9 leaves, claim slots, pins, `build.py`; driver and `SimWorld`; chain, differential and mutation suites, XCH and CAT rewards (§12) | Done on the simulator. Still open: the distributor registry, a `clvmPuzzleAudit.md` pass by a second reader |
| **3: testnet lane** | Router (quote + create, our key), operator bot, watcher, API, forge-ui tab, on `Config.testnet` distributors | One campaign end to end on testnet11: create → roots → claims at several points → reclaim of leftovers |
| **4: hardening** | Public dispute leaf, guardian as a Forge multisig safe, external review | No mainnet before external review |

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

Run everything with `python -m pytest -q` (166 tests, about 7 s) after `python contracts/build.py`.

| Suite | Proves |
|---|---|
| `contracts/build.py` | Every puzzle compiles with rue 0.8.4; the five upstream puzzles reproduce slot-machine@2d37ba1's published hashes; our eight leaves match `pins.json` |
| `tests/test_chain.py` | On the simulator, for XCH **and** a CAT reward: only the holder or their delegate can claim (no authorization, someone else's message, someone else's key and an unknown mode are all refused; the holder's signature and message both pay; a hot wallet claims for a cold wallet and a vault after a grant, only the holder grants or revokes, revoked and ungranted delegates are refused, one hop only; only the creator or the creator's delegate reclaims, and a holder's delegate is not the creator's), the full lifecycle (3% fee, claims at several points, late claim past the window, leftovers to the creator, fee collection), the reserve coin equal to state after every step, several actions in one spend, and a refusal probe per rule (router, deposit, lengths, claim window, duplicates, operator, ceiling, sequence, tiling, totals, veto, rotate, forged claims, slot inserts, double claims, height pin) |
| `tests/test_differential.py` | Twelve random scenarios (alternating XCH and CAT) through the reference model and the puzzles side by side: 48 roots, 80 paid claims, 45 refused claims, reclaims and collects. Every payout and every refusal agrees, and the reserve matches after each step |
| `scripts/mutate.py` | Deleting any single assertion in our leaves makes a test fail (38 of 45), or does not compile (5, enforced by rue's type narrowing), or is argued in `scripts/mutation-arguments.json` (2: `amount > 0` and `budget > 0` imply each other while the fee is below 100%) |

A refusal counts only if the leaf raised (`clvm raise`) or consensus rejected the bundle (`Simulator
error`). A Python error in a test or the driver is a bug and fails the test.

**What the first mutation run found.** With every test green, 25 of 37 buildable mutants survived. Three
of the unpinned assertions guard attacks: the slot ordering check (a double claim), `end <= h` in
`post_root` (a root dated in the future releases budget early), and the height pin (a later action naming
a later height reclaims early). The assertions were in the puzzles; the tests that proved them were not.
Each now has a probe built so that the assertion it names is the first thing that can refuse it.

**Not yet proven:**
- **Real consensus.** The simulator runs chia_rs consensus checks, but testnet11 is the gate (phase 3).
- **Cost under load.** Spend cost with many campaigns in state, and with many claims in one spend, is not
  measured yet. `max_campaigns` bounds the state.
- **Real keys.** Router, operator and guardian are anyone-can-spend test puzzles here. On testnet they
  are a wallet key (router, operator) and a Forge multisig safe (guardian).
- **The distributor registry** is not built.
- **No second reader.** Nothing here has been reviewed by anyone but its author.
