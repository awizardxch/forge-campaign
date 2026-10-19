# FLPCR — Agent tools

> The contract for agents (the aWizard agent, Spellbook, a testnet operator agent) that drive FLPCR.
> Each tool lists its inputs, outputs, **who signs**, what the agent must check first, and where it is
> implemented today. The design is `SPEC.md`; HTTP shapes are `API.md`; the on-chain rules are
> `PUZZLES.md`.
>
> Status: every tool's spend logic exists and is tested on the simulator (`flpcr/chain/sim.py`
> `SimWorld`, `flpcr/chain/driver.py` `Distributor`). The live versions (real keys, node push, the HTTP
> API) are the testnet work in `TESTNET.md`.

## Ground rules for every tool

1. **Never sign for a user.** User tools return an unsigned bundle plus a signing request (Sage
   `chip0002_signCoinSpends` or `signMessageByAddress`). Only the user's wallet signs.
2. **Show before signing:** the amounts, the reward asset, every recipient puzzle hash, and the
   network fee.
3. **Build against the current singleton coin.** Messages are bound to one coin. If the distributor moved
   since the bundle was built, rebuild.
4. **One spend must not touch the same slot twice.** Split claims for adjacent new holders across
   spends.
5. **Report refusals as they come.** A `clvm raise` names a puzzle rule; a `Simulator error` /
   mempool error names a consensus rule. Never retry a refused bundle unchanged.
6. **Testnet first.** Use `Config.testnet` distributors (8-block disputes, campaigns from 16 blocks).
   Nothing goes to mainnet before an external review.

## User tools

### `flpcr.quote`

Validates a campaign and prices it. Nothing is signed.

- **In:**
  - `creator` (address), `reward_asset` (XCH, or a CAT asset id), `amount` (mojos, fee included);
  - `kind` (`LP_HOLD` or `CAT_HOLD`), `target` (pool launcher id or CAT asset id);
  - `start`, `end` (heights) or `length`, and `claim_window` (blocks);
  - optional `params`: `time_bps` / `fees_bps`, `min_balance`, lists, `gates`, `boosts`,
    `boost_cap_bps`, `fee_window` (blocks, default 4608; SPEC §6.3).
- **Checks:**
  - `flpcr.router.check_campaign`: the reward asset and any CAT target trade in a Forge registry pool;
    an LP target is a registered pool.
  - `flpcr.model.Campaign` validation.
  - The distributor's bounds: length, and claim window ≥ 2 × `dispute_blocks`.
- **Out:**
  - `campaign_id`, `fee` (3%, paid to the dev multisig at creation), `budget`, per-day emission,
    `reclaim_from`;
  - the offer to make: give `amount` of `reward_asset`, request nothing.
- **Code:** `Campaign(...)`, `router.check_campaign`, `Distributor` config bounds.

### `flpcr.create`

Submits a quoted campaign.

- **In:** the quote and the creator's signed Sage offer.
- **Signs:**
  - **the creator**, the offer (the deposit);
  - **our router key**, the `"c" + campaign_id` message. This is server-side only; never expose the
    router key to an agent that talks to users.
- **Out:** the spend bundle id. After confirmation, the dev multisig holds `fee`, and the reserve has
  grown by `budget`.
- **Code:** `SimWorld.create` / `create_raw`. Live: offer settlement as the deposit.

### `flpcr.rewards`

Read-only. What an address has earned and can claim.

- **In:** `address`, optional `reward_asset`.
- **Out:** per campaign:
  - `earned` (engine), `cumulative` (active root), `claimed` (claim slot), `claimable`, `pending`
    (dispute window);
  - `qualifies` and `multiplier_bps` (gates and boosts);
  - `reclaim_from`, `reclaimed`, and the merkle proof.
- **Code:** `Root.claim_for`, `Distributor.read_state`, the claim slot records.

### `flpcr.claim`

Claims an address's rewards.

- **In:** `address`, optional `campaigns`, optional `delegate`.
- **Signs** one of:
  - **the holder**: `AGG_SIG_ME(synthetic_key, leaf)` for a standard address (mode 0), or a message
    `"k" + leaf` from the holder's own puzzle for a vault or multisig (mode 1);
  - **the granted delegate**, signing the same way for itself (mode 2).

  `leaf = sha256tree(["flpcr-claim-v1", distributor_id, campaign_id, address, cumulative])`.
- **Out:** the unsigned bundle and signing request. The payout always goes to `address`.
- **Check:** `claimable > 0`. The campaign is not reclaimed (a claim past `reclaim_from` is still valid
  until the reclaim lands).
- **Code:** `SimWorld.claim` (mode 1), `claim_signed` (mode 0), `claim_delegated` (mode 2).

### `flpcr.delegate`

Grants or revokes a delegate that may claim, or reclaim, for an address. This is for a cold wallet with a
hot or cloud wallet, or a vault with a signer.

- **In:** `holder`, `delegate`, `grant` (true or false).
- **Signs:** **the holder only**, over
  `sha256tree(["flpcr-delegate-v1", distributor_id, holder, delegate, grant])`, by mode 0 or 1. A
  delegate can never grant itself.
- **Out:** the unsigned bundle. A grant is per distributor, i.e. per reward asset.
- **Code:** `SimWorld.set_delegate`.

### `flpcr.reclaim`

Returns a campaign's leftovers to its creator.

- **In:** `campaign_id`, optional `delegate`.
- **Signs:** **the creator**, or the creator's granted delegate, over
  `sha256tree(["flpcr-reclaim-v1", distributor_id, campaign_id])`.
- **Check:** `h ≥ end + claim_window`. The refund (`budget − claimed`) always goes to the creator.
- **Code:** `SimWorld.reclaim`.

### `flpcr.link`

Joins two of a holder's addresses for gates and boosts.

- **In:** `distributor`, `primary`, `member` (both the user's addresses), `seq` (one above the pair's
  last signed entry, from `GET /links`), and `unlink` to undo a link.
- **Signs:** a link, **both addresses**, `signMessageByAddress` over
  `flpcr-link-v2:<distributor_id>:<primary>:<member>:<seq>`. An unlink, **either address** alone, over
  `flpcr-unlink-v2:<distributor_id>:<primary>:<member>:<seq>`.
- **Check:** `flpcr.links.verify(link)` before submitting, and a `seq` above the pair's last.
- **Out:** the entry, appended to the distributor's link log at the current height. Each root manifest
  commits the log prefix it used, and the log is published beside it.
- **Code:** `flpcr.links` (`sign`, `verify`, `link_to_json`, `log_to_json`, `log_hash`, `LinkBook`).

## Operator tools (our keys, never user-facing)

### `flpcr.engine.run`

Computes the next root. Nothing is signed.

- **In:** a distributor, its live campaigns (not yet reclaimed), and `end ≤ peak`.
- **Out:** a root manifest (`flpcr/manifest.py`) with `root`, `totals`, every proof and the link log
  commitment (`links.count`, `links.sha256`), plus the committed log prefix to publish beside it. If
  `run_epoch` returns no root, nothing claimable changed: skip posting, and the next run covers the
  longer window.
- **Check:** the engine's own ceiling assertion, and the link log check (a log that rewrites what an
  earlier root committed, or backdates an entry before its `end`, raises). Either failure is a bug or
  tampering: stop, don't post.
- **Code:** `flpcr.engine.run_epoch`, `manifest.root_to_json`, `links.log_to_json`.

### `flpcr.post_root`

Posts a root.

- **Signs:** **the operator key** (`engine_ph`), message
  `"p" + sha256tree([root_id, start, end, root, totals, manifest_hash])`.
- **Check:** there is no pending root (wait out the dispute window), `start == active.end`, and
  `root_id = active + 1`.
- **Cadence:** post as soon as the previous root activates. Post the root covering a campaign's `end`
  within a few blocks of `end`; the claim window may be short.
- **Code:** `SimWorld.post`.

### `flpcr.watch`

Checks each pending root.

- **Does:** recompute the pending root from chain data and the published link log with the engine
  (checking the log against `links.sha256` in the manifest); on any mismatch, alert and request
  a guardian veto inside `dispute_blocks` (testnet 8, production 38). This must be automated; a human
  cannot react in 12 minutes.
- **Signs (veto):** **the guardian** (a Forge multisig safe), message `"v" + sha256tree(root_id)`.
- **Code:** `run_epoch` replay plus `SimWorld.veto`.

### `flpcr.rotate`

Replaces the operator key.

- **Signs:** **the guardian**, message `"r" + new_engine_ph`.
- **Code:** `SimWorld.rotate`.

## Deployment (once per reward asset)

`launch` and then `init`. Curry `dev_fee_ph` (the Speechless dev multisig), `router_ph`,
`guardian_ph`, `engine_ph`, `fee_bps = 300` and the clocks. **All of these are fixed for the
distributor's life, except the operator key (`rotate`).**

A CAT distributor needs a seed coin of the reward CAT for its empty reserve. Save the launcher id and the
driver state; never commit keys or that state.

**Code:** `Distributor.launch_spends`, `SimWorld.__init__`.
