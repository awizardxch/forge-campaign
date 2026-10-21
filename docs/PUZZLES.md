# FLPCR — Distributor Puzzles (as built)

> The reference for `contracts/`. Read with `SPEC.md` §5 and §12. **The Rue sources are the authority**;
> where this text and `contracts/puzzles/flpcr/*.rue` disagree, the source wins and this file is
> wrong. `flpcr/distributor.py` is the same rules in Python and is held to the puzzles by
> `tests/test_differential.py`.

## Build

```bash
cargo install rue-cli --version 0.8.4 --locked   # Forge's version; reproduces upstream's hashes
pip install chia-wallet-sdk==0.36.0 pytest
python contracts/build.py            # compile to contracts/compiled/*.hex, refuse on pin drift
python contracts/build.py --check    # CI: exit 1 if compiled/ is stale or any pin moved
python contracts/build.py --repin    # after an intended leaf change; never touches upstream pins
python scripts/mutate.py             # the mutation run, ~3 min
```

`contracts/Rue.toml` names 0.8.4. Upstream's own `Rue.toml` says 0.9.0, but 0.8.4 reproduces every
upstream hash; `build.py` checks that on every build.

## Layout

```
contracts/
  puzzles/upstream/      verbatim from Yakuhito/slot-machine@2d37ba1: action, finalizer,
                         reserve_finalizer, slot, p2_delegated_by_singleton, merkle_utils,
                         common_types, slot helpers. Never edited; pinned by tree hash and sha256.
  puzzles/flpcr/         shared.rue + ten leaves + bond_launcher.rue (a plain coin puzzle, not a leaf)
  compiled/*.hex         build output, committed
  pins.json              upstream tree hashes (published values), our leaf and bond_launcher hashes,
                         upstream source sha256s
flpcr/leaves.py          the one leaf list (LEAVES) plus EXTRA = [bond_launcher]: build.py,
                         scripts/mutate.py and the driver all read it
```

`build.py` enforces both upstream pins on every build: the compiled tree hash must equal the published
value, and the sha256 of every upstream source (line endings normalized, so a CRLF checkout passes)
must equal `upstream_sources_sha256` in `pins.json`. An edit that happens to compile to the same tree
hash still fails. `--repin` rewrites only our leaf hashes.

## Shape

```
distributor  = singleton_top_layer_v1_1( action_layer( reserve_finalizer, LEAVES_ROOT, State ) )
reserve      = p2_delegated_by_singleton(SINGLETON_MOD_HASH, struct_hash, nonce 0)
               wrapped in CAT(reward_asset) for a CAT reward, bare for XCH
claim slot   = slot(SingletonInfo, nonce 1)(sha256tree(ClaimSlot))
delegation   = slot(SingletonInfo, nonce 2)(sha256tree(DelegationSlot))
bond coin    = p2_delegated_by_singleton(SINGLETON_MOD_HASH, struct_hash, nonce 3), bare XCH, holding
               exactly dispute_bond; its puzzle hash is Config.bond_puzzle_hash (driver BOND_NONCE = 3)
bond launcher= bond_launcher.rue, a plain coin the disputer's own spend creates with exactly
               dispute_bond mojos; its hash is Config.bond_launcher_hash
LEAVES_ROOT  = merkle root of the ten curried leaf hashes, in this order (flpcr/leaves.py):
               init, create, post_root, veto, claim, reclaim, rotate, delegate, dispute, resolve
```

The reserve finalizer is curried with `(ACTION_LAYER_MOD_HASH, RESERVE_FULL_PH, RESERVE_INNER_PH, 2,
launcher_id)`, then with its own hash. The `2` is the program `(f State)`: the reserve amount is the
first state field.

Every leaf is curried with the **same** `Config`, so the leaves cannot disagree about which distributor
they belong to. A registry entry rebuilds `LEAVES_ROOT` from one `Config`, which is how a reader checks
that a distributor is genuine (Forge's M-4 lesson).

## Types (`shared.rue`)

```
Config   [distributor_id, reward_asset, fee_bps, dev_fee_ph, admin_ph, guardian_ph, router_ph,
          dispute_blocks, rotate_delay, delegate_fee, min_campaign_length, max_campaign_length,
          max_campaigns, slot_1st_curry_hash, delegation_slot_1st_curry_hash,
          dispute_bond, bond_puzzle_hash, bond_launcher_hash]
State    [reserve, engine_ph, initialized, campaigns, active, pending, rotation, dispute]
Campaign [id, creator, budget, start, end, claim_window, claimed, excluded]   excluded = sha256tree(blacklist)
Root     [root_id, start, end, root, totals, activates_at]      totals = ((id . total) ...), ascending ids
Rotation [engine_ph, effective_at]                              State.rotation is nil or a Rotation
Dispute  [root_id, disputer_ph, bond_parent]                    State.dispute is nil or a Dispute
Truth    (ephemeral . State)                                    ephemeral = nil or (h)
ClaimSlot      (key . (claimed . (left . right)))               key = sha256tree((campaign_id . puzzle_hash))
DelegationSlot (key . (active . (uses . (left . right))))       key = sha256tree((owner . delegate))
```

`admin_ph` is the dev multisig, normally the same address as `dev_fee_ph`. `dispute_bond` is in mojos of
XCH whatever the reward asset (production 5 XCH = 5,000,000,000,000; testnet 0.1 TXCH =
100,000,000,000); `bond_puzzle_hash` is the distributor's `p2_delegated_by_singleton` at nonce 3, where a
bond sits; `bond_launcher_hash` is the hash of `bond_launcher.rue`, the only puzzle a bond coin may be
born from. `Dispute.bond_parent` is the bond launcher's coin id, from which `resolve` derives the bond
coin.

A leaf receives `(Truth . solution)` and returns `(Truth' . conditions)`. A condition `(-42 . c)` is
spent by the reserve: the finalizer gathers them into the reserve's delegated puzzle.

`HEIGHT_WINDOW = 32`. Every rule that counts blocks from `h` toward something another party relies on
(the dispute window, the rotation delay, a campaign's earliest start) adds it, so a leaf naming an `h`
up to 31 blocks old can never shorten that rule.

`BURN_PUZZLE_HASH = 0x00…dead`, Chia's burn address, where Forge pools burn their locked LP floor: it
holds LP but is no one's position, so no campaign ever pays it. `MAX_EXCLUDED = 16`: the most addresses
a creator can exclude from one campaign (every claim reveals the list, so it stays short). Two list
helpers:

- `is_member(list, x)`: whether `x` is in `list`;
- `count_hashes(list)`: asserts every item is 32 bytes and returns how many there are.

## The prologue

Every solution starts with `h`.

- **First action of a spend** (ephemeral is nil):
  - assert `h > 0`;
  - emit `ASSERT_HEIGHT_ABSOLUTE h` and `ASSERT_BEFORE_HEIGHT_ABSOLUTE h + 32`;
  - promote `pending` to `active` if `h >= pending.activates_at` **and no dispute is open** (a disputed
    root waits for `resolve`, however long that takes);
  - adopt a pending rotation if `h >= rotation.effective_at`: `engine_ph = rotation.engine_ph`,
    `rotation = nil`;
  - store `(h)` as the ephemeral state.
- **Later actions:** assert `h` equals the stored one.

Promotion and adoption are lazy: they happen on the next spend. If nobody spends the singleton after
`activates_at`, the guardian's veto can still land up to 31 blocks later, because its `h` may trail the
tip.

## Authorization (`shared.rue`)

`auth` values: `0` signature, `1` message, `2` delegate, `3` admin (reclaim only).

- `authorize(address, auth, data, message)` accepts:
  - `auth == 0`: `data` is a synthetic public key. Assert
    `curry_tree_hash(STANDARD_MOD_HASH, [tree_hash_atom(key)]) == address` and emit
    `AGG_SIG_ME(key, message)`;
  - `auth == 1`: emit `RECEIVE_MESSAGE` mode 23 of `"k" + message` from `address`;
  - anything else is refused.
- `authorize_claim(address, auth, data, message)`, for claims only, names no generation:
  - `auth == 0`: `data` is a synthetic public key. Assert
    `curry_tree_hash(STANDARD_MOD_HASH, [tree_hash_atom(key)]) == address` and emit
    `AGG_SIG_UNSAFE(key, message)` (no coin at `address` needed);
  - `auth == 1`: emit `RECEIVE_MESSAGE` mode 16 (`MODE_PUZZLE_TO_ANY`: sender by puzzle hash, receiver
    uncommitted) of `"k" + message` from `address`. One send still matches exactly one receive;
  - anything else is refused.
- `authorize_by_spend(address, auth, message)` accepts only `auth == 1`: the action must cost its
  author a coin spend. `create` uses it.
- `authorize_or_delegate(config, owner, auth, data, message, spend_only)`:
  - `auth == 2`: `data = (delegate, uses, left, right, inner_auth, ...inner_data)`. Spend the delegation
    slot `(key, 1, uses, left, right)` and create `(key, 1, uses + 1, left, right)`, with
    `key = sha256tree((owner . delegate))`; then the delegate authorizes by `authorize_by_spend` (if
    `spend_only`) or `authorize`. Neither accepts `auth == 2`, so a delegate cannot delegate. The
    recreated value always differs from the spent one, so a slot the same spend creates can never prove
    its own grant.
  - otherwise the owner authorizes directly, by `authorize_by_spend` or `authorize`.

## Leaves

All messages are `RECEIVE_MESSAGE` mode 23 (sender puzzle hash → this coin), except a claim's
authorization, which is mode 16 (sender puzzle hash, receiver uncommitted).

### `init` `[h]`
- `initialized == 0`.
- Check the `Config` once: `rotate_delay > dispute_blocks`, `delegate_fee >= 0`, `dispute_bond > 0`
  (a free dispute would be a free freeze of every root), and `bond_puzzle_hash`, `bond_launcher_hash`,
  `admin_ph` and `dev_fee_ph` are 32 bytes.
- Create the sentinel slots of both lists:
  - claims (nonce 1): `(0x00.., 0, 0x00.., 0xff..)` and `(0xff.., 0, 0x00.., 0xff..)`;
  - delegations (nonce 2): `(0x00.., 0, 0, 0x00.., 0xff..)` and `(0xff.., 0, 0, 0x00.., 0xff..)`.
- Set `initialized = 1`.

### `create` `[h, creator, amount, start, end, kind, target, params, nonce, claim_window, auth]`
1. Derive `id = sha256tree(["flpcr-campaign-v1", distributor_id, creator, reward_asset, amount, start, end, kind, target, sha256tree(params), nonce, fee_bps, claim_window])`.
2. Compute `fee = amount * fee_bps / 10000` and `budget = amount − fee`.
3. Check:
   - `initialized == 1`, and `creator` is 32 bytes;
   - `amount > 0` and `budget > 0`;
   - `h + 32 <= start <= h + max_campaign_length`, and `min_campaign_length <= end − start <= max_campaign_length`;
   - `kind ∈ {1, 2, 3}` (`LP_HOLD`, `CAT_HOLD`, `NFT_HOLD`);
   - for `params = (time_bps fees_bps min_balance blacklist . rest)` (the order of `Params.as_clvm`):
     both bps `>= 0` and they sum to 10000; `kind == 1` or `fees_bps == 0` (so only an LP campaign
     weights by fees);
   - `count_hashes(blacklist) <= MAX_EXCLUDED`: at most 16 excluded addresses, each 32 bytes;
   - `3 * dispute_blocks + 32 <= claim_window <= max_campaign_length`;
   - fewer than `max_campaigns` campaigns, and `id` is new.
4. Receive `"c" + id` from `router_ph`, **and** `"k" + id` from `creator` (`authorize_by_spend`: `auth`
   must be 1; a signature is refused).
5. Update state: `reserve += budget`, and prepend the campaign with `claimed = 0` and
   `excluded = sha256tree(blacklist)`. The blacklist is also inside `sha256tree(params)`, so the id
   commits to it. If `fee > 0`, pay it now:
   `(-42 . (CREATE_COIN dev_fee_ph fee (dev_fee_ph)))`. The deposit is
   enforced by conservation (SPEC §5.2). A CAT ring carries no slack, so a CAT deposit returns any
   excess as CAT change.

### `post_root` `[h, root_id, start, end, root, totals, manifest_hash]`
1. Check the window:
   - `pending` is nil;
   - `start < end <= h`.
2. Check the sequence:
   - with no active root, `root_id == 1`;
   - otherwise `root_id == active.root_id + 1` and `start == active.end`.
3. Check each `(id . total)`, in strictly ascending id order:
   - the campaign is live;
   - `total >= previous total`;
   - `total <= emittable(campaign, end)`, where `emittable` clamps `end` to `[campaign.start, campaign.end]`.
4. Every live campaign in the active root's totals must appear in the new totals.
5. Receive `"p" + sha256tree([root_id, start, end, root, totals, manifest_hash])` from `engine_ph`
   (after the prologue's adoption, so a rotation whose delay has passed is already in force).
6. Set `pending = Root(…, activates_at = h + 32 + dispute_blocks)`: claimable at least `dispute_blocks`
   after the block that includes it.

### `veto` `[h, root_id]`
- `pending` is not nil and `pending.root_id == root_id`. The prologue has already promoted a root whose
  window passed, so a veto after promotion finds nothing pending.
- `dispute` is nil: a disputed root is settled by `resolve`, which refunds or forfeits the bond.
- Receive `"v" + sha256tree(root_id)` from `guardian_ph`.
- Set `pending = nil`.

### `claim` `[h, campaign_id, puzzle_hash, cumulative, proof, insert, slot_info, auth, auth_data, excluded]`
1. `puzzle_hash` is 32 bytes, there is an active root, and the campaign is live (there is no height
   check). Then, in this order and before the proof:
   - `puzzle_hash != BURN_PUZZLE_HASH`;
   - `sha256tree(excluded) == campaign.excluded` (the list `create` stored; a shorter list fails);
   - `!is_member(excluded, puzzle_hash)`.
2. `simplify_merkle_proof(leaf, proof) == active.root`, where
   `leaf = sha256tree(["flpcr-claim-v1", distributor_id, campaign_id, puzzle_hash, cumulative])`.
3. **Only the holder or their delegate, naming no generation** (`authorize_claim`):
   - `auth == 0`: `auth_data` is the holder's synthetic key; `AGG_SIG_UNSAFE(key, leaf)`.
   - `auth == 1`: a coin at `puzzle_hash` sends `"k" + leaf`, mode 16.
   - `auth == 2`: `auth_data = (delegate, uses, left, right, auth, data)`; the holder's delegation slot
     is spent and recreated with `uses + 1`, and the delegate authorizes the leaf by `auth == 0` or
     `auth == 1` at `delegate`.
   - Anything else is refused. Replay is harmless: the leaf names the distributor and the cumulative,
     the slot records `claimed`, and step 5 asserts `amount > 0`; the payout always goes to
     `puzzle_hash`.
4. Let `key = sha256tree((campaign_id . puzzle_hash))`.
   - **`insert == 0`**: `slot_info = (claimed . (left . right))`. Spend the key's slot and recreate it
     with `claimed = cumulative`.
   - **`insert == 1`**: `slot_info = [left_key, left_claimed, left_left, right_key, right_claimed, right_right]`.
     Assert `left_key < key < right_key`. Spend both neighbours, whose values name each other, and
     recreate them pointing at `key`. Create `(key, cumulative, left_key, right_key)`.
5. Let `amount = cumulative − previous`. Assert `amount > 0` and `claimed_c + amount <= active.totals[campaign_id]`.
6. Pay `(-42 . (CREATE_COIN puzzle_hash amount (puzzle_hash)))`.
7. Set `claimed_c += amount` and `reserve -= amount`; `excluded` is kept unchanged.

### `reclaim` `[h, campaign_id, auth, auth_data]`
- The campaign is live and `h >= end + claim_window`.
- Let `message = sha256tree(["flpcr-reclaim-v1", distributor_id, campaign_id])`. Authorized by either:
  - **the creator:** `authorize_or_delegate(creator, auth, auth_data, message, spend_only = false)`,
    i.e. the creator's signature or message, or a delegate the creator granted through `delegate`
    (itself by signature or message);
  - **the admin:** `auth == 3`, and a coin at `admin_ph` (the dev multisig) sends `"k" + message`.
- If `budget − claimed > 0`, pay it **to the creator**, whoever authorized it.
- Remove the campaign and set `reserve -= that`.

### `delegate` `[h, owner, delegate, grant, insert, slot_info, auth, auth_data]`
1. `grant ∈ {0, 1}`; `owner` and `delegate` are 32 bytes.
2. `authorize(owner, auth, auth_data, sha256tree(["flpcr-delegate-v2", distributor_id, owner, delegate, grant]))`.
   The owner, by signature or message; never the delegate.
3. Let `key = sha256tree((owner . delegate))`.
   - **`insert == 1`** (first grant for the key; `grant` must be 1, so a revoke of a never-granted key
     is refused): `slot_info = [left_key, left_active, left_uses, left_left, right_key, right_active,
     right_uses, right_right]`. Assert `left_key < key < right_key`; spend both neighbours and recreate
     them pointing at `key`; create `(key, 1, 0, left_key, right_key)`.
   - **`insert == 0`**: `slot_info = (active . (uses . (left . right)))`. Spend
     `(key, active, uses, left, right)` and create `(key, grant, uses + 1, left, right)`.

   One slot per key, so granting twice and revoking once leaves the delegate revoked.
4. Emit `RESERVE_FEE delegate_fee`: every grant or revoke leaves that much as network fee.
5. State is unchanged. Delegation slots are hinted with their key.

### `rotate` `[h, new_engine_ph, cancel]`
- **`cancel == 0`:** `new_engine_ph` is 32 bytes. Receive `"r" + new_engine_ph` from `guardian_ph`.
  Set `rotation = (new_engine_ph, h + 32 + rotate_delay)`, replacing any pending one (the delay
  restarts). `engine_ph` is unchanged until the prologue adopts the rotation.
- **`cancel == 1`:** after the prologue's adoption, a rotation is still pending and its `engine_ph`
  equals `new_engine_ph`. Receive `"x" + new_engine_ph` from `admin_ph`. Set `rotation = nil`.
- Anything else is refused.

### `dispute` `[h, root_id, disputer_ph, bond_grandparent]`
Anyone; no authorization. The bond is the authorization, and `disputer_ph` is only where a refund goes
(the disputer's own choice). Merkl's dispute, on coins.
1. After the prologue's promotion: `pending` is not nil and `pending.root_id == root_id` (a root past its
   window is already active and can no longer be disputed); `dispute` is nil (one open dispute at a
   time); `disputer_ph` is 32 bytes; `bond_grandparent != 0x00..`.
2. Derive `bond_parent = coinid(bond_grandparent, bond_launcher_hash, dispute_bond)`: the bond launcher,
   a child of the disputer's own coin, with exactly `dispute_bond` mojos.
3. Emit `ASSERT_COIN_ANNOUNCEMENT sha256(bond_parent + "flpcr-bond-v1" + sha256tree([bond_puzzle_hash,
   dispute_bond, distributor_id]))`. Only a coin whose id commits to `bond_launcher_hash` can make that
   announcement, and that puzzle does nothing but create the bond, so a bond that was never funded, or
   funded with the wrong amount, cannot open a dispute (the Forge registry's reserve-launcher pattern).
4. Set `dispute = (root_id, disputer_ph, bond_parent)`. `pending` is unchanged and stays pending: the
   prologue skips promotion while a dispute is open, so the root cannot activate until `resolve` lands.

### `bond_launcher` `[created_puzzle_hash, amount, distributor_id]` (not a leaf)
A plain coin puzzle, compiled and pinned beside the leaves (`EXTRA` in `flpcr/leaves.py`). The disputer's
own coin spend creates it with exactly `dispute_bond` mojos of bare XCH, hinted with the distributor id,
in the same bundle as the `dispute` action.
- `amount > 0`; `ASSERT_MY_AMOUNT amount`.
- `CREATE_COIN created_puzzle_hash amount (distributor_id)`: the bond coin, at `bond_puzzle_hash`
  (`p2_delegated_by_singleton`, nonce 3), for the whole amount. Bare XCH even for a CAT distributor; it
  never enters the reserve.
- `CREATE_COIN_ANNOUNCEMENT "flpcr-bond-v1" + sha256tree([created_puzzle_hash, amount, distributor_id])`,
  which `dispute` asserts.

### `resolve` `[h, upheld, sender]`
1. `dispute` and `pending` are both not nil; `upheld` is 0 or 1; `sender` is `guardian_ph` or `admin_ph`.
2. Receive `"s" + sha256tree([dispute.root_id, upheld])` from `sender`.
3. Let `bond_coin = coinid(dispute.bond_parent, bond_puzzle_hash, dispute_bond)` and
   `recipient = disputer_ph` if upheld, else `dev_fee_ph`.
4. Emit `SEND_MESSAGE` mode 23 to `bond_coin` with message
   `sha256tree((q . [[51, recipient, dispute_bond, [recipient]]]))`, the tree hash of the one delegated
   puzzle that pays the bond out, hinted. The bundle must spend the bond coin with exactly that puzzle
   (`p2_delegated_by_singleton` receives the hash from this singleton); nothing else can be run against
   it, and the message names the coin by id, so it cannot be replayed against another bond.
5. **Upheld (`1`):** `pending = nil`; the operator reposts a corrected root under the same id. The bond
   is refunded to `disputer_ph`.
6. **Rejected (`0`):** `pending` is kept with `activates_at = h + 32 + dispute_blocks` (the window
   restarts, so a bad dispute buys no time). The bond is forfeited to `dev_fee_ph`.
7. Either way `dispute = nil`.

While a dispute is open, `post_root` is refused for want of a nil `pending` and `veto` is refused
outright; claims against the previous active root continue. Only one dispute at a time.

## Driver conventions (`flpcr/chain/driver.py`)

- **Flow:**
  - a per-leaf builder on `Distributor` (`init`, `create`, `post_root`, `veto`, `rotate`,
    `cancel_rotation`, `claim`, `reclaim`, `delegate`, `dispute`, `resolve`) returns a `LeafCall`: the
    action, the `Message`s and `Signature`s the bundle must carry, the network fee the leaf reserves,
    the slot records it changes, `creations` (coins the caller's own spend must create, as `(puzzle
    hash, amount, memo)`) and `extra_spends` (spends the driver builds beside the singleton's);
  - `prepare(calls) -> Prepared` runs every action locally to thread the state and returns the coin
    spends (`extra_spends` included), the new state, the required messages, signatures and fee, the
    slot updates, the `creations`, and any unresolved slots;
  - `apply(prepared)` advances the driver once the bundle has confirmed.
- **Authorization** is typed (`flpcr/chain/auth.py`): `BySignature`, `ByMessage`, `ByDelegate`,
  `ByAdmin`. The driver never signs or spends a wallet coin; the caller satisfies each `Message` (a
  coin at `sender` sends it, mode 23; mode 16 for a claim) and `Signature` (an `AGG_SIG_ME` against
  the singleton coin; `unsafe`, an `AGG_SIG_UNSAFE` over the leaf, for a claim).
- **Collisions:** `prepare` raises `BundleCollision` for actions that cannot share one singleton spend:
  two reserve payments of the same amount to the same puzzle hash (one coin id twice), or two actions
  touching the same claim or delegation slot, including a shared neighbour. Split them across spends.
- **Sequencing claims:** `sequence(h, requests, max_cost=5_000_000_000, per_generation=25) -> ClaimBatch`
  takes `ClaimRequest(campaign_id, puzzle_hash, cumulative, proof, auth)` items (one per campaign and
  holder, highest cumulative wins) and packs them into chained generations of the singleton in one
  bundle, each generation spending what the previous one created, until `spend_cost` (CLVM + 12,000
  per byte + 1,800,000 per `CREATE_COIN` + 1,200,000 per `AGG_SIG_*`) would pass `max_cost`. The rest
  is `deferred`; requests the leaf refuses are `rejected`. The driver is left unchanged:
  `apply_batch(batch)` advances it after confirmation, and `reloaded()` gives a fresh allocator
  between bundles.
- **Excluded lists:** `Distributor.excluded` records each campaign's excluded list (its params'
  blacklist) at `create()`, and `claim(..., excluded=None)` passes that list by default; snapshot and
  restore carry it. A driver restored without it would build claims the leaf refuses.
- **State** is read through `ChainState` (`flpcr/chain/state.py`), typed and in `shared.rue` field order.
- **Action-layer solution:** `(puzzles . (selectors_and_proofs . (solutions . reserve_parent_id)))`.
  - `puzzles` lists each distinct leaf once.
  - A selector is the path to its leaf in that list: 2, 5, 11, … (`2^(i+1) + 2^i − 1`).
  - `selectors_and_proofs` is in **reverse** execution order and carries a proof on every entry.
  - `solutions` is one per action, in execution order.
- **Reserve:**
  - Solution: `(singleton_inner_ph . (delegated_puzzle . nil))`.
  - Delegated puzzle: `(q . (recreate . tagged))`, where `tagged` is each action's `-42` conditions
    **reversed**, concatenated in execution order (the finalizer prepends as it walks).
  - For a CAT reward, the reserve and any deposit are spent in one CAT ring (`Clvm.spend_cats`).
- **Bonds (`BOND_NONCE = 3`):**
  - `Distributor.dispute(h, root_id, disputer_ph, disputer_coin_id)` names the disputer's coin as the
    bond grandparent. Its `creations` lists the bond launcher `(bond_launcher_hash, dispute_bond,
    launcher_id)` that the disputer's own spend of that coin must create, and its `extra_spends` holds
    the launcher's spend (solution `[bond_ph, dispute_bond, launcher_id]`), which creates the bond coin
    and announces it. The driver never spends the disputer's coin; `SimWorld.dispute` and
    `scripts/deploy_testnet.py` build that spend from `Prepared.creations`.
  - `Distributor.resolve(h, upheld, sender)` (sender defaults to the guardian) reads the open dispute
    from `chain_state`, carries the `"s"` message, and adds the bond coin's spend to `extra_spends`:
    `Coin(bond_parent, bond_ph, dispute_bond)` with the bond puzzle and the solution
    `[singleton_inner_ph, (q . [[51, recipient, dispute_bond, [recipient]]])]`. Any other delegated
    puzzle fails consensus (`MessageNotSentOrReceived`).
  - The bond is bare XCH for every distributor; it is never part of the reserve or a CAT ring.
- **Slot spends:**
  - Puzzle: `slot_1st.curry(value_hash)`, with the claim (nonce 1) or delegation (nonce 2) `slot_1st`.
  - Solution: `((creator_parent . (creator_inner_ph . 1)) . spender_inner_ph)`, where the creator is the
    singleton generation that created the slot.
  - The driver adds a spend for every `SEND_MESSAGE` mode 18 a leaf emits. An unknown slot is listed in
    `Prepared.unresolved` and left out, so consensus refuses the bundle; a real builder treats a
    non-empty list as "resync first".
  - A chain reader must accept a slot only if its parent is a known distributor generation: anyone can
    create a 0-mojo look-alike at a slot's puzzle hash with the same hint (it cannot be spent as a slot).
- **`Clvm.alloc` gotcha:** it encodes a list of small ints as a byte string. Values are built
  explicitly in `flpcr/chain/values.py`.
- **Conservation is bundle-wide:** an XCH deposit can be topped up by any spare mojo in the bundle.
  The test harness's message coins recreate themselves, so the bundle carries no slack and a short
  deposit is refused.
- **Test stand-ins:** `Keys` (anyone-can-spend labeled puzzles) and `send_message_condition` live in
  `flpcr/chain/sim.py`, not the driver.

## Still to build

- **Registry.** Key `sha256tree((reward_asset . router_ph))`. Rebuilds `LEAVES_ROOT` from `Config` and
  pins `fee_bps = 300`, the `dispute_blocks` and `rotate_delay` floors, `dev_fee_ph` and `admin_ph`.
- **Cost measurements.** State with `max_campaigns` entries, and many claims per spend, on the
  re-pinned leaves and for a CAT reward.
- **A `clvmPuzzleAudit.md` pass by a second reader.**
