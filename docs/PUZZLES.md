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
  puzzles/flpcr/         shared.rue + nine leaves
  compiled/*.hex         build output, committed
  pins.json              upstream hashes (published values), our leaf hashes, upstream source sha256s
```

## Shape

```
distributor  = singleton_top_layer_v1_1( action_layer( reserve_finalizer, LEAVES_ROOT, State ) )
reserve      = p2_delegated_by_singleton(SINGLETON_MOD_HASH, struct_hash, nonce 0)
               wrapped in CAT(reward_asset) for a CAT reward, bare for XCH
claim slot   = slot(SingletonInfo, nonce 1)(sha256tree(slot_value))
LEAVES_ROOT  = merkle root of the nine curried leaf hashes, in this order:
               init, create, post_root, veto, claim, reclaim, collect, rotate, delegate
delegation   = slot(SingletonInfo, nonce 2)(sha256tree((holder . delegate)))
```

The reserve finalizer is curried with `(ACTION_LAYER_MOD_HASH, RESERVE_FULL_PH, RESERVE_INNER_PH, 2,
launcher_id)`, then with its own hash. The `2` is the program `(f State)`: the reserve amount is the
first state field.

Every leaf is curried with the **same** `Config`, so the leaves cannot disagree about which distributor
they belong to. A registry entry rebuilds `LEAVES_ROOT` from one `Config`, which is how a reader checks
that a distributor is genuine (Forge's M-4 lesson).

## Types (`shared.rue`)

```
Config   [distributor_id, reward_asset, fee_bps, treasury_ph, guardian_ph, router_ph,
          dispute_blocks, min_campaign_length, max_campaign_length, max_campaigns, slot_1st_curry_hash,
          delegation_slot_1st_curry_hash]
State    [reserve, fees_owed, engine_ph, initialized, campaigns, active, pending]
Campaign [id, creator, budget, start, end, claim_window, claimed]
Root     [root_id, start, end, root, totals, activates_at]      totals = ((id . total) ...), ascending ids
Truth    (ephemeral . State)                                    ephemeral = nil or (h)
ClaimSlot (key . (claimed . (left . right)))
```

A leaf receives `(Truth . solution)` and returns `(Truth' . conditions)`. A condition `(-42 . c)` is
spent by the reserve: the finalizer gathers them into the reserve's delegated puzzle.

## The prologue

Every solution starts with `h`.

- **First action of a spend** (ephemeral is nil):
  - assert `h > 0`;
  - emit `ASSERT_HEIGHT_ABSOLUTE h` and `ASSERT_BEFORE_HEIGHT_ABSOLUTE h + 32`;
  - promote `pending` to `active` if `h >= pending.activates_at`;
  - store `(h)` as the ephemeral state.
- **Later actions:** assert `h` equals the stored one.

## Leaves

All messages are `RECEIVE_MESSAGE` mode 23 (sender puzzle hash → this coin).

### `init` `[h]`
- `initialized == 0`.
- Create the two sentinel slots, `(0x00.., 0, 0x00.., 0xff..)` and `(0xff.., 0, 0x00.., 0xff..)`.
- Set `initialized = 1`.

### `create` `[h, creator, amount, start, end, kind, target, params, nonce, claim_window]`
1. Derive `id = sha256tree(["flpcr-campaign-v1", distributor_id, creator, reward_asset, amount, start, end, kind, target, sha256tree(params), nonce, fee_bps, claim_window])`.
2. Compute `fee = amount * fee_bps / 10000` and `budget = amount − fee`.
3. Check:
   - `amount > 0` and `budget > 0`;
   - `start >= h`, and `min_campaign_length <= end − start <= max_campaign_length`;
   - `kind ∈ {1, 2}`;
   - for `params = (time_bps fees_bps …)`: both `>= 0` and they sum to 10000; `kind == 1` or `fees_bps == 0`;
   - `2 * dispute_blocks <= claim_window <= max_campaign_length`;
   - fewer than `max_campaigns` campaigns, and `id` is new.
4. Receive `"c" + id` from `router_ph`.
5. Update state: `reserve += amount`, `fees_owed += fee`, and prepend the campaign. The deposit is
   enforced by conservation (SPEC §5.2).

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
   - `total <= emittable(campaign, end)`.
4. Every live campaign in the active root's totals must appear in the new totals.
5. Receive `"p" + sha256tree([root_id, start, end, root, totals, manifest_hash])` from `engine_ph`.
6. Set `pending = Root(…, activates_at = h + dispute_blocks)`.

### `veto` `[h, root_id]`
- `pending` is not nil and `pending.root_id == root_id`. The prologue has already promoted a root whose
  window passed, so a late veto finds nothing pending.
- Receive `"v" + sha256tree(root_id)` from `guardian_ph`.
- Set `pending = nil`.

### `claim` `[h, campaign_id, puzzle_hash, cumulative, proof, insert, slot_info, auth, auth_data]`
1. There is an active root, and the campaign is live (there is no height check).
2. `simplify_merkle_proof(leaf, proof) == active.root`, where
   `leaf = sha256tree(["flpcr-claim-v1", distributor_id, campaign_id, puzzle_hash, cumulative])`.
   **Only the holder or their delegate can claim** (`authorize` in `shared.rue`):
   - `auth == 0`: `auth_data` is a synthetic public key. Assert
     `curry_tree_hash(STANDARD_MOD_HASH, [tree_hash_atom(key)]) == puzzle_hash` and emit
     `AGG_SIG_ME(key, leaf)`.
   - `auth == 1`: emit `RECEIVE_MESSAGE` mode 23 of `"k" + leaf` from `puzzle_hash`.
   - `auth == 2`: `auth_data = (delegate . (mode . data))`. Spend and recreate the delegation slot
     `(puzzle_hash . delegate)`, then `authorize(delegate, mode, data, leaf)`, which accepts only modes
     0 and 1, so a delegate cannot delegate.
   - Any other value is refused.
3. Let `key = sha256tree((campaign_id . puzzle_hash))`.
   - **`insert == 0`**: `slot_info = (claimed . (left . right))`. Spend the key's slot and recreate it
     with `claimed = cumulative`.
   - **`insert == 1`**: `slot_info = [left_key, left_claimed, left_left, right_key, right_claimed, right_right]`.
     Assert `left_key < key < right_key`. Spend both neighbours, whose values name each other, and
     recreate them pointing at `key`. Create `(key, cumulative, left_key, right_key)`.
4. Let `amount = cumulative − previous`. Assert `amount > 0` and `claimed_c + amount <= active.totals[campaign_id]`.
5. Pay `(-42 . (CREATE_COIN puzzle_hash amount (puzzle_hash)))`.
6. Set `claimed_c += amount` and `reserve -= amount`.

### `reclaim` `[h, campaign_id]`
- The campaign is live and `h >= end + claim_window`.
- If `budget − claimed > 0`, pay it to the creator.
- Remove the campaign and set `reserve -= that`.

### `delegate` `[h, holder, delegate, grant, auth, auth_data]`
1. `grant ∈ {0, 1}`.
2. `authorize(holder, auth, auth_data, sha256tree(["flpcr-delegate-v1", distributor_id, holder, delegate, grant]))`.
   The holder, never the delegate.
3. If `grant == 1`, create the delegation slot `(holder . delegate)`, hinted with the holder. If
   `grant == 0`, spend it (refused if it does not exist).
4. State is unchanged.

### `collect` `[h]`
- `fees_owed > 0`.
- Pay it to `treasury_ph`, then set `reserve -= fees_owed` and `fees_owed = 0`.

### `rotate` `[h, new_engine_ph]`
- Receive `"r" + new_engine_ph` from `guardian_ph`.
- Set `engine_ph = new_engine_ph`.

## Driver conventions (`flpcr/chain/driver.py`)

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
- **Slot spends:**
  - Puzzle: `slot_1st.curry(value_hash)`.
  - Solution: `((creator_parent . (creator_inner_ph . 1)) . spender_inner_ph)`, where the creator is the
    singleton generation that created the slot.
  - The driver adds a spend for every `SEND_MESSAGE` mode 18 a leaf emits. An unknown slot is left out,
    so consensus refuses the bundle, rather than the driver refusing it first.
- **`Clvm.alloc` gotcha:** it encodes a list of small ints as a byte string. Values are built
  explicitly in `flpcr/chain/values.py`.
- **Conservation is bundle-wide:** an XCH deposit can be topped up by any spare mojo in the bundle.
  The test harness's message coins recreate themselves, so the bundle carries no slack and a short
  deposit is refused.

## Still to build

- **Registry.** Key `sha256tree((reward_asset . router_ph))`. Rebuilds `LEAVES_ROOT` from `Config` and
  pins `fee_bps = 300`, the `dispute_blocks` floor and `treasury_ph`.
- **Cost measurements.** State with `max_campaigns` entries, and many claims per spend.
- **A `clvmPuzzleAudit.md` pass by a second reader.**
