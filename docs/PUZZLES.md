# FLPCR — Distributor Puzzles (build spec)

> The phase 2 build spec, in the shape the V14 specs took. Nothing here is written in Rue yet. It is
> meant to be built in `contracts/flpcr/` on the Forge workstation, where `rue` 0.8.4, `chia_rs` and
> the V14 harness live. Read with `SPEC.md` §5, `chip0050ActionLayer.md` and `clvmPuzzleAudit.md`.
> **`flpcr/distributor.py` is the executable form of this document.** Where the two disagree, fix
> one of them before writing Rue, and every scenario in `tests/test_distributor.py` becomes a puzzle
> suite case.

## Shape

```
distributor = singleton( action_layer( reserve_finalizer, LEAVES_ROOT, state ) )
reserve     = p2_delegated_by_singleton(distributor_launcher, nonce 0), bare XCH or CAT(reward_asset)
claim slot  = slot(distributor_launcher, nonce 1)(sha256tree(slot_value))
```

- `action_layer`, `reserve_finalizer`, `p2_delegated_by_singleton` and `slot` are the upstream
  builds, hash-pinned. **None is edited.** The reserve finalizer's `RESERVE_AMOUNT_PROGRAM` is
  `flpcr_reserve_amount.rue`: `state -> state.reserve_total`.
- Seven leaves, each curried with the same `Config`. This is the M-4 lesson: the finalizer is curried
  with `CONFIG_HASH` and the leaf mod hashes, and rebuilds the root every spend. Copy V14's
  `config_root`.
- A shared prologue runs in the first action only:
  - `h > last_height`, `ASSERT_HEIGHT_ABSOLUTE h`, `ASSERT_BEFORE_HEIGHT_ABSOLUTE h + 32`;
  - `ASSERT_MY_BIRTH_HEIGHT birth` with `h >= birth`, and `ASSERT_MY_AMOUNT 1`;
  - **promotion**: if `pending` and `h >= pending.activates_at`, then `active = pending; pending = nil`.

  Later actions read `h` from ephemeral state.
- Every solution is a proper list of fixed fields, `[h, …]`. **Derived, never claimed**: any value the
  puzzle can compute (campaign id, fee, budget, settlement coin id, claim leaf, slot puzzle hash) is
  computed, not read from the solution.

## Config (curried)

| Field | Value / bound |
|---|---|
| `distributor_id` | the launcher id; it is also in every claim leaf |
| `reward_asset` | `ZERO_32` = XCH |
| `treasury_ph` | the protocol treasury |
| `fee_bps` | **300** (3%). The registry pins it, so every registered distributor charges the same |
| `guardian_ph` | intended: a Forge multisig safe's puzzle hash |
| `dispute_blocks` | default 1,152 (≈ 6 h); `>= MIN_DISPUTE_BLOCKS` (384) |
| `claim_window` | default 138,240 (30 days); `>= MIN_CLAIM_WINDOW` (7 days) |
| `max_campaigns` | e.g. 32; bounds the state hashed every spend |
| `max_duration` | e.g. 4,608 × 365 blocks |

## State

```
[engine_ph, campaigns, active, pending, fees_owed, reserve_total, last_height]
campaign = (id, creator_ph, budget, start, end, claimed)
root     = (root_id, start, end, root, totals, activates_at)       totals = ((campaign_id . total) ...) sorted
```

The invariant every leaf must preserve, and that a suite checks after every leaf on random state, is
`reserve_total == Σ (budget − claimed) + fees_owed` (`Distributor.check_invariant`).

## Claim slots

A slot value is `(key . (claimed . (left . right)))`, with `key = sha256tree((campaign_id . puzzle_hash))`
and sentinels `0x00…` and `0xff…` created by the launcher. This is the Forge registry's sorted-slot
pattern, with `claimed` where the registry keeps `launcher`. Adjacency is structural: a slot names its
neighbours' keys.

- **First claim for a key:** spend the left neighbour `L` with `L.key < key < L.right`. Recreate `L` with
  `right = key`, recreate the right neighbour's left pointer the same way, and create the new slot with
  `claimed = cumulative`. Two slots for one key would need an insert between equal keys, which the strict
  `<` refuses.
- **Later claims:** spend the key's own slot and recreate it with the new `claimed`. The pointers are
  unchanged.

Slots are spent with the singleton's message, as in upstream, so only a distributor spend can touch
them.

## Leaves

### `create` `[h, campaign_fields, params, settle_parent, settle_amount]`

1. Rebuild `params_hash = sha256tree(params)` and
   `id = sha256tree(["flpcr-campaign-v1", distributor_id, creator, reward_asset, amount, start, end, kind, target, params_hash, nonce, fee_bps])`.
   `fee_bps` comes from config, not the solution.
2. Check the struct:
   - `amount > 0`, `fee = amount * fee_bps / 10000`, `budget = amount − fee > 0`;
   - `start >= h`, `start < end <= start + max_duration`, `kind ∈ {1, 2}`;
   - `time_bps + fees_bps == 10000`, and `kind == 2 ⇒ fees_bps == 0`;
   - `len(campaigns) < max_campaigns`, and `id` not already present.

   The params list shape is checked; its contents are not (the engine interprets them).
3. Bind the deposit with `settlement_binding(reward_asset, settle_parent, settle_amount, amount)` (V14 §2:
   derived id, `ASSERT_CONCURRENT_SPEND`, `settle_amount >= amount`).
4. Update state:
   - append `(id, creator, budget, start, end, 0)`;
   - `fees_owed += fee + (settle_amount − amount)`. The slack is kept, not dropped, so the reserve matches
     the coins;
   - `reserve_total += settle_amount`.
5. Emit `CREATE_PUZZLE_ANNOUNCEMENT ["flpcr-created", id]` for indexers.

### `post_root` `[h, root_id, start, end, root, totals, manifest_hash]`

1. `RECEIVE_MESSAGE` mode 23 from a coin at `engine_ph`, with content
   `sha256tree(["flpcr-post", distributor_id, root_id, start, end, root, totals, manifest_hash])`.
2. `pending == nil`. After the prologue's promotion, a root still in its dispute window blocks the post.
3. Let `last = active`. Check the sequence and window:
   - `root_id == (last ? last.root_id + 1 : 1)`;
   - `start == last.end` when there is a `last`;
   - `start < end <= h`.
4. Check the totals:
   - `totals` is sorted by id with no duplicates;
   - every campaign in `last.totals` that is still live appears in `totals`;
   - for each `(c . t)`: `c` is live, `t >= last.totals[c]` (0 if absent), and
     `t <= budget_c * (clamp(end, start_c, end_c) − start_c) / (end_c − start_c)`.
5. `pending = (root_id, start, end, root, totals, h + dispute_blocks)`. Neither `reserve_total` nor the
   campaigns change.

### `veto` `[h, root_id]`

1. `RECEIVE_MESSAGE` mode 23 from `guardian_ph`, with content
   `sha256tree(["flpcr-veto", distributor_id, root_id])`.
2. `pending` exists, `pending.root_id == root_id` and `h < pending.activates_at`.
3. `pending = nil`. The operator re-posts under the same id.

### `claim` `[h, campaign_id, puzzle_hash, cumulative, path, hashes, slot_proof]`

`slot_proof` is either `(own . (claimed . (left . right)))` or `(neighbour . (key . (right . left_of_right)))`.

1. `active` exists. The campaign is live and `h < end_c + claim_window`.
2. `simplify_merkle_proof(sha256tree(["flpcr-claim-v1", distributor_id, campaign_id, puzzle_hash, cumulative]), (path . hashes)) == active.root`.
3. Read `prev` from the slot proof: the own slot's `claimed`, or `0` for an insert, with adjacency checked.
4. `amount = cumulative − prev > 0`, and `claimed_c + amount <= active.totals[campaign_id]`.
5. Update state: `claimed_c += amount`, `reserve_total -= amount`, and the slot's `claimed = cumulative`.
6. Reserve condition: `(-42 . (CREATE_COIN puzzle_hash amount (puzzle_hash)))`.
7. Several claims in one spend run as several actions. Equal `(puzzle_hash, amount)` pairs in one spend
   would be `DUPLICATE_OUTPUT` (the V13 `collect` lesson), so the driver merges or splits them across
   spends.

### `reclaim` `[h, campaign_id]`

1. The campaign is live and `h >= end_c + claim_window`.
2. `amount = budget − claimed`. If `amount > 0`, emit reserve `CREATE_COIN creator_ph amount (creator_ph)`.
3. Remove the campaign and set `reserve_total -= amount`. Later claims are refused by "campaign is live",
   and later roots must leave it out of `totals`.

### `collect` `[h]`

1. `fees_owed > 0`. Pay it to `treasury_ph` (hinted).
2. Set `fees_owed = 0` and `reserve_total -= it`.

### `rotate` `[h, new_engine_ph]`

1. A message from `guardian_ph`, with content `sha256tree(["flpcr-rotate", distributor_id, new_engine_ph])`.

## Registry

Copy `forge_registry_{common,init,register}` with key = `reward_asset`, one distributor per asset.
`register` rebuilds the distributor's full puzzle hash from config, so a registry entry **is** a known-good
config. `RegistryConstants` pins:
- `fee_bps = 300`;
- the `dispute_blocks` and `claim_window` floors;
- `treasury_ph`.

The launcher creates the 0-mojo reserve and the two claim-slot sentinels. `register` proves both, which
is V14's R-1 lesson: derived, never claimed.

## Suites to write (V14 conventions)

| Suite | Proves |
|---|---|
| `_test_flpcr_integrity.py` | every puzzle recompiles to its pinned hash, and upstream is untouched |
| `_test_flpcr_reference.py` | every scenario in `tests/test_distributor.py` gives the same result (payments and refusals) on the puzzles |
| `_test_flpcr_ceiling.py` | `post_root` refuses one mojo above `emittable(end)` at the boundaries (before start, start, mid, end, after end) and any decreasing total |
| `_test_flpcr_claims.py` | double claim (same root, same spend, two spends); stale root after promotion; another distributor's or campaign's proof; claim past the posted total; insert of an existing key; claim after the window |
| `_test_flpcr_reclaim.py` | reclaim inside the window, twice, and with claims racing in the same block |
| `_test_flpcr_invariant.py` | the `reserve_total` identity after random leaf sequences |
| `scripts/mutate-flpcr.py` | every assertion killed, or reported UNREACHED with a written argument |
