# FLPCR — Agent tools

> The contract for agents (the aWizard agent, Spellbook, a testnet operator agent) that drive FLPCR.
> Each tool lists its inputs, outputs, **who signs**, what the agent must check first, and where it is
> implemented today. The design is `SPEC.md`; HTTP shapes are `API.md`; the on-chain rules are
> `PUZZLES.md`.
>
> Status: every tool's spend logic exists and is tested on the simulator (`flpcr/chain/sim.py`
> `SimWorld`, `flpcr/chain/driver.py` `Distributor`). Each spend is built the same way: a per-leaf
> builder on `Distributor` returns a `LeafCall`, `prepare(calls)` returns a `Prepared` (the coin spends
> plus the messages, signatures and network fee the bundle must carry), and `apply(prepared)` advances
> the driver once the bundle confirms. Authorization is typed (`flpcr/chain/auth.py`: `BySignature`,
> `ByMessage`, `ByDelegate`, `ByAdmin`). The live versions (real keys, node push, the HTTP API) are the
> testnet work in `TESTNET.md`.

## Ground rules for every tool

1. **Never sign for a user.** User tools return an unsigned bundle plus a signing request (Sage
   `chip0002_signCoinSpends` or `signMessageByAddress`). Only the user's wallet signs.
2. **Show before signing:** the amounts, the reward asset, every recipient puzzle hash, and the
   network fee.
3. **Build against the current singleton coin.** Messages are bound to one coin, except a claim's
   authorization, which names no generation. If the distributor moved since the bundle was built,
   rebuild (a claim's signature or message can be reused as is).
4. **Split what collides.** One singleton spend must not touch the same claim or delegation slot twice,
   and that includes a shared neighbour: two first claims whose keys fall between the same two slots
   collide, and so do two claims through one delegate. Nor may it carry two reserve payments of the same
   amount to the same puzzle hash (one coin id twice). `Distributor.prepare` refuses such a batch with
   `BundleCollision`; split it across spends. For claims, `Distributor.sequence` splits them into
   chained generations of one bundle.
5. **Report refusals as they come, and don't guess the rule.** Every puzzle refusal is the same
   `clvm raise`, with no rule named; say which leaf refused and what the inputs were. A
   `Simulator error` / mempool error names its consensus rule (for example
   `AssertHeightAbsoluteFailed`); report that name. Never retry a refused bundle unchanged.
6. **Testnet first.** Use `Config.testnet` distributors (8-block disputes, campaigns from 16 blocks,
   claim windows from 56 blocks, starts at least 32 blocks after the tip). Nothing goes to mainnet
   before an external review.

## User tools

### `flpcr.quote`

Validates a campaign and prices it. Nothing is signed.

- **In:**
  - `creator` (address), `reward_asset` (XCH, or a CAT asset id), `amount` (mojos, fee included);
  - `kind` (`LP_HOLD`, `CAT_HOLD` or `NFT_HOLD`), `target` (pool launcher id, CAT asset id, or for
    `NFT_HOLD` a minter DID or a `col1` collection id: `collection_id(did, metadata collection.id)`,
    SPEC §6.8);
  - `start`, `end` (heights) or `length`, and `claim_window` (blocks);
  - optional `params`: `time_bps` / `fees_bps` (`fees_bps` must be 0 unless `LP_HOLD`), `min_balance`,
    lists (`blacklist`: up to 16 **excluded addresses** the campaign never pays), `gates`, `boosts`,
    `boost_cap_bps`, `fee_window` (blocks, default 4608; SPEC §6.3).
- **Checks:**
  - `flpcr.router.check_campaign`: the reward asset and any CAT target trade in a Forge registry pool;
    an LP target is a registered pool; an NFT target is on `RouterRules.nft_collections` when that
    list is non-empty.
  - `flpcr.model.Campaign` validation (`MAX_EXCLUDED = 16`, each entry 32 bytes; the `create` leaf
    checks the same).
  - The distributor's bounds (`flpcr.distributor.Config`):
    - `tip + 32 <= start <= tip + max_campaign_length` (`Config.min_start_lead`); quote against the
      tip, and leave margin for the time the bundle takes to land;
    - `min_campaign_length <= end − start <= max_campaign_length`;
    - `3 × dispute_blocks + 32 <= claim_window <= max_campaign_length` (`Config.min_claim_window`:
      testnet 56, production 146);
    - `creator` is a 32-byte puzzle hash.
- **Out:**
  - `campaign_id`, `fee` (3%, paid to the dev multisig at creation), `budget`, per-day emission,
    `reclaim_from`;
  - the spend to sign: one of the creator's own coins, at `creator`, supplies `amount` of
    `reward_asset` and sends `"k" + campaign_id` to the distributor.
- **Code:** `Campaign(...)`, `router.check_campaign`, `Config` bounds.

### `flpcr.create`

Submits a quoted campaign.

- **In:** the quote and the creator's signed coin spend.
- **Signs:**
  - **the creator**, a spend of a coin at `creator` that supplies the deposit and sends
    `"k" + campaign_id` (mode 23) to the current singleton coin. A signature alone, or a "request
    nothing" offer, is refused: `create` needs the creator's own coin spend;
  - **our router key**, the `"c" + campaign_id` message. This is server-side only; never expose the
    router key to an agent that talks to users.
- **Out:** the spend bundle id. After confirmation, the dev multisig holds `fee`, and the reserve has
  grown by `budget`.
- **Check:** the distributor has run `init` (`create` refuses until it has). For a CAT reward, any
  deposit above `amount` must come back as a CAT change output; the CAT ring carries no slack.
- **Code:** `Distributor.create` (a `LeafCall` carrying both messages), `SimWorld.create` /
  `create_raw`.

### `flpcr.rewards`

Read-only. What an address has earned and can claim.

- **In:** `address`, optional `reward_asset`.
- **Out:** per campaign:
  - `earned` (engine), `cumulative` (active root), `claimed` (claim slot), `claimable`, `pending`
    (dispute window);
  - `qualifies` and `multiplier_bps` (gates and boosts);
  - `reclaim_from`, `reclaimed`, and the merkle proof.
- **Code:** `Root.claim_for`, `Distributor.chain_state` (a typed `ChainState`), the claim slot records.

### `flpcr.claim`

Claims an address's rewards.

- **In:** `address`, optional `campaigns`, optional `delegate`.
- **Signs** one of (none names a generation of the distributor):
  - **the holder's key** (mode 0, standard addresses): `AGG_SIG_UNSAFE(key, leaf)` by the synthetic key
    whose standard puzzle hash is `address`. No coin at `address` is needed. A wallet signs every
    address's claims in one `chip0002_signCoinSpends` request for a never-pushed, zero-amount stand-in
    spend `(q . ((49 key leaf) ...))` (the pattern of Forge's fee proof);
  - **the holder's coin** (mode 1, any puzzle: vault, multisig): a spend of a coin at `address` that
    sends `"k" + leaf`, `RECEIVE_MESSAGE` mode 16;
  - **the granted delegate** (mode 2): the holder's delegation slot is spent and recreated with
    `uses + 1`, and the delegate authorizes the leaf by key signature or by coin spend at its own address.

  `leaf = sha256tree(["flpcr-claim-v1", distributor_id, campaign_id, address, cumulative])`. A replayed
  signature or message pays nothing (the slot records `claimed`; `amount > 0` is asserted), and no other
  distributor accepts it. One coin spend of the claimer's wallet pays the network fee.
- **Out:** the unsigned bundle and signing request. The payout always goes to `address`, whoever
  submits the bundle.
- **Check:** `claimable > 0`. The campaign is not reclaimed (a claim past `reclaim_from` is still valid
  until the reclaim lands). A mode-1 claimer holds a coin at the authorizing address. Claims that collide
  (rule 4) go in chained generations of one bundle (`Distributor.sequence`). `address` is not the burn
  address and not on the campaign's excluded list: every claim reveals that list, and the leaf checks it
  against the hash stored at creation, then refuses a listed address.
- **Code:** `Distributor.claim` with `BySignature(key)`, `ByMessage()` or `ByDelegate(delegate, via)`
  (its `excluded=None` defaults to the list `Distributor.excluded` recorded at `create()`);
  `Distributor.sequence(h, [ClaimRequest(...)])` for many holders, `apply_batch` after confirmation;
  `SimWorld.claim`, `claim_delegated`; `deploy_testnet.py claim --by-key` and `claim-all`.

### `flpcr.delegate`

Grants or revokes a delegate that may claim, or reclaim, for an address. This is for a cold wallet with a
hot or cloud wallet, or a vault with a signer.

- **In:** `owner`, `delegate`, `grant` (true or false).
- **Signs:** **the owner only**, over
  `sha256tree(["flpcr-delegate-v2", distributor_id, owner, delegate, grant])`, by signature (mode 0) or
  message (mode 1). A delegate can never grant itself.
- **Pays:** every grant or revoke leaves `delegate_fee` as network fee (testnet 1,000,000 mojos,
  production 50,000,000), on top of the usual fee. `Prepared.fee` says how much.
- **Out:** the unsigned bundle. A grant is per distributor, i.e. per reward asset. The first grant
  inserts a delegation slot (hinted with `sha256tree((owner . delegate))`); later grants and revokes
  recreate it. Granting twice and revoking once leaves it revoked; revoking a pair that was never
  granted is refused.
- **Code:** `Distributor.delegate`, `SimWorld.set_delegate`.

### `flpcr.reclaim`

Returns a campaign's leftovers to its creator.

- **In:** `campaign_id`, optional `delegate`.
- **Signs** one of, over `sha256tree(["flpcr-reclaim-v1", distributor_id, campaign_id])`:
  - **the creator**, by signature (mode 0) or a message from the creator's address (mode 1);
  - **the creator's granted delegate**, by signature or message (mode 2);
  - **the admin** (the dev multisig): see Admin tools.
- **Check:** `h ≥ end + claim_window`. The refund (`budget − claimed`) always goes to the creator,
  whoever submits it. Before offering the reclaim, also apply the reclaim guard (SPEC §5.5; operator
  policy, not a puzzle rule): refuse while the emission is not fully posted, while a root carrying the
  campaign is pending, or while the final root has been claimable for less than `claim_window` blocks
  and holders still have unclaimed amounts in it.
- **Code:** `Distributor.reclaim`, `SimWorld.reclaim`; the guard is `reclaim_blockers()` in
  `scripts/deploy_testnet.py` (`reclaim --force` overrides).

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

### `flpcr.dispute`

Contests a pending root with a bond. **Anyone** may call it; the puzzle asks for no authorization, the
bond is the authorization. This is Merkl's dispute (anyone stakes the dispute token, a multisig resolves)
with the stake as a coin.

- **In:** `distributor`, `root_id` (the pending root), `disputer` (the address a refund goes to; the
  disputer's own choice), and the disputer's coin that will fund the bond (at least `dispute_bond` plus
  the network fee).
- **Signs:** **the disputer**, a spend of their own coin that creates the **bond launcher**: a coin at
  `bond_launcher_hash` holding exactly `dispute_bond` mojos of XCH (production 5 XCH =
  5,000,000,000,000, testnet 0.1 TXCH = 100,000,000,000), hinted with the distributor id, with the rest
  as change. XCH whatever the reward asset. The launcher's own spend and the singleton's are built by the
  driver and ride in the same bundle: the launcher creates the bond coin at the distributor's
  `bond_puzzle_hash` (`p2_delegated_by_singleton`, nonce 3) and announces it, and the `dispute` action
  asserts that announcement from the launcher id it derives from the disputer's coin. A bond that was
  never funded, or funded with the wrong amount, is refused on chain.
- **Check:** the root is pending (`ChainState.pending.root_id == root_id`; a root past its window is
  promoted by the next spend and can no longer be disputed), no dispute is open (`ChainState.dispute`
  is nil; one at a time), and the disputer has replayed the root (`flpcr.watch`) and found a mismatch:
  a rejected dispute forfeits the bond.
- **Out:** the unsigned bundle (`Prepared.spends`, which includes the launcher's spend) plus
  `Prepared.creations`, the `(bond_launcher_hash, dispute_bond, distributor_id)` coin the disputer's spend
  must create. After confirmation `ChainState.dispute = (root_id, disputer_ph, bond_parent)` and the root
  stays pending; it cannot activate until `flpcr.resolve` lands. Veto is refused meanwhile, and so is
  posting; claims against the previous active root continue.
- **Code:** `Distributor.dispute(h, root_id, disputer_ph, disputer_coin_id)` (a `LeafCall` with
  `creations` and `extra_spends`), `SimWorld.dispute`, `scripts/deploy_testnet.py dispute`.

## Operator tools (our keys, never user-facing)

### `flpcr.engine.run`

Computes the next root. Nothing is signed.

- **In:** a distributor, its live campaigns (not yet reclaimed), a `Source`, and `end ≤ peak`. On
  testnet11 the source is `Combined(LiveLpSource, ChainNftSource(NodeReader, collections))`: every LP
  holder crawled from the chain (time share only for now) and the holders of each `NFT_HOLD`
  collection. Every holder is in every root, claimed or not.
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
  `root_id = active + 1`. The sender is the operator key in force at `h`: a pending rotation whose
  `effective_at` has passed is adopted by this very spend (`Distributor.engine_at(h)`).
- **Cadence:** post as soon as the previous root activates. A root activates at
  `h + 32 + dispute_blocks`, so the fastest cadence is one root every `dispute_blocks + 32` blocks. Post
  the root covering a campaign's `end` within a few blocks of `end`; the claim window may be short.
- **Code:** `Distributor.post_root`, `SimWorld.post`.

### `flpcr.watch`

Checks each pending root.

- **Does:** recompute the pending root from chain data and the published link log with the engine
  (checking the log against `links.sha256` in the manifest); on any mismatch, alert and request
  a guardian veto inside `dispute_blocks` (testnet 8, production 38). This must be automated; a human
  cannot react in 12 minutes. Anyone else running the watcher disputes instead (`flpcr.dispute`), which
  holds the root until the guardian or admin resolves.
- **Signs (veto):** **the guardian** (a Forge multisig safe), message `"v" + sha256tree(root_id)`. A
  veto is refused while a dispute is open (`ChainState.dispute`); use `flpcr.resolve`.
- **Also watches disputes:** an open `ChainState.dispute` freezes the pending root and posting. Replay
  the disputed root and resolve it promptly, either way.
- **Also watches rotations:** a pending `rotation` in state (`ChainState.rotation`) is a proposed
  operator key. Alert on any proposal; if it is not expected, ask the admin to cancel it before
  `effective_at`.
- **Code:** `run_epoch` replay plus `Distributor.veto` / `SimWorld.veto`.

### `flpcr.rotate`

Proposes a new operator key.

- **Signs:** **the guardian**, message `"r" + new_engine_ph` (32 bytes).
- **Effect:** stores `rotation = (new_engine_ph, h + 32 + rotate_delay)` (production `rotate_delay` 4,608
  blocks ≈ one day, testnet 24). The current operator keeps posting until the first spend at or after
  `effective_at` adopts the new key. A newer proposal replaces the old one and restarts the delay.
- **Code:** `Distributor.rotate`, `SimWorld.rotate`.

### `flpcr.resolve`

Settles an open dispute. **The guardian or the admin** (see Admin tools); nobody else.

- **In:** `distributor`, `upheld` (`1`: the disputer was right; `0`: the dispute was wrong), `sender`
  (`guardian_ph`, the default, or `admin_ph`).
- **Signs:** **the guardian or the admin**, a spend of a coin at its address that sends
  `"s" + sha256tree([root_id, upheld])` (mode 23) to the current singleton coin, where `root_id` is the
  disputed root's. The driver adds the **bond coin's spend** to the bundle: the coin
  `coinid(bond_parent, bond_puzzle_hash, dispute_bond)` runs the one delegated puzzle
  `(q . [[51, recipient, dispute_bond, [recipient]]])` whose tree hash the singleton sends it (mode 23).
  Any other puzzle fails consensus (`MessageNotSentOrReceived`).
- **Effect:**
  - **upheld:** `pending = nil`; the operator reposts a corrected root under the same id
    (`flpcr.post_root`). The bond is refunded to the address the disputer named.
  - **rejected:** the root stays pending with `activates_at = h + 32 + dispute_blocks` (the window
    restarts; a bad dispute buys no time). The bond is forfeited to `dev_fee_ph`, the dev multisig.
  - either way `dispute = nil`. The bond can go nowhere else: neither the guardian nor the admin can
    take it.
- **Check:** a dispute is open and the root is still pending (`ChainState.dispute`, `ChainState.pending`);
  `upheld` is exactly 0 or 1; replay the root first and uphold only a real mismatch.
- **Code:** `Distributor.resolve(h, upheld, sender)` (a `LeafCall` with the message and the bond spend in
  `extra_spends`), `SimWorld.resolve`, `scripts/deploy_testnet.py resolve --upheld 0|1`.

## Admin tools (the dev multisig, never user-facing)

The admin is `admin_ph`, the dev multisig, normally the same address as `dev_fee_ph`. It cannot move
funds anywhere but to a campaign's creator (a forfeited bond reaches it only through `flpcr.resolve`,
which the leaf routes to `dev_fee_ph` itself).

### `flpcr.resolve` (as admin)

The same tool as above with `sender = admin_ph`: the admin's coin sends `"s" + sha256tree([root_id,
upheld])`. **Code:** `Distributor.resolve(h, upheld, sender=admin_ph)`, `SimWorld.resolve(upheld,
sender="admin")`.

### `flpcr.cancel_rotation`

Cancels a pending operator rotation.

- **Signs:** **the admin**, message `"x" + engine_ph`, where `engine_ph` is the pending rotation's key.
- **Check:** a rotation is pending, names that key, and its `effective_at` has not passed (a spend at or
  after it adopts the rotation first, and the cancel is refused).
- **Code:** `Distributor.cancel_rotation`, `SimWorld.cancel_rotation`.

### `flpcr.reclaim` (as admin)

Reclaims a campaign for its creator, so a fully claimed or abandoned campaign does not hold a table slot
forever.

- **Signs:** **the admin**, message `"k" + sha256tree(["flpcr-reclaim-v1", distributor_id,
  campaign_id])` (auth 3).
- **Check:** `h ≥ end + claim_window`, the same as the creator, and the same reclaim guard. The refund
  always goes to the creator.
- **Code:** `Distributor.reclaim(..., ByAdmin())`, `SimWorld.reclaim(..., admin=True)`.

## Deployment (once per reward asset)

`launch` and then `init`. Curry one `Config` (`flpcr/distributor.py`): `dev_fee_ph` and `admin_ph`
(the Speechless dev multisig), `router_ph`, `guardian_ph`, `fee_bps = 300`, the clocks
(`dispute_blocks`, `rotate_delay`, campaign length bounds), `delegate_fee`, `max_campaigns` and
`dispute_bond` (production 5 XCH, testnet 0.1 TXCH; the driver derives `bond_puzzle_hash` and
`bond_launcher_hash` itself); the operator key `engine_ph` starts in the state. `init` refuses a
`Config` with `rotate_delay <= dispute_blocks`, a negative `delegate_fee`, a zero `dispute_bond`, or a
malformed `bond_puzzle_hash`, `bond_launcher_hash`, `admin_ph` or `dev_fee_ph`, and no campaign can be
created before it runs. **All of these are fixed for the distributor's life,
except the operator key (`rotate`, delayed and cancellable).**

A CAT distributor needs a seed coin of the reward CAT for its empty reserve. Save the launcher id and the
driver state; never commit keys or that state.

**Code:** `Distributor.launch_spends`, `SimWorld.__init__`.
