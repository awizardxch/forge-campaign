# FLPCR — HTTP API (draft)

The read side is modeled on Merkl's v4 (`/opportunities`, `/campaigns`, `/users/{address}/rewards`),
so integrators who know Merkl find their way.
- Base URL: `https://<host>/api/flpcr/v1`.
- Amounts are strings of mojos, the Forge snapshot convention, so they survive `JSON.parse`.
- Addresses accept `xch1…`/`txch1…` or a 64-hex puzzle hash, and responses carry both.

## Read

### `GET /opportunities?kind=&asset=&pool=&status=live|upcoming|ended`

One row per target with at least one campaign:

```json
{ "target": "<pool launcher | cat asset id | minter DID | col1 collection id>", "kind": "LP_HOLD", "name": "TXCH/wUSDC",
  "tvl_xch": "1234500000000", "apr_bps": 2310,
  "daily_rewards": [{ "asset": "<id>", "symbol": "wUSDC", "amount": "41000000" }],
  "campaigns": ["<campaign id>", "..."] }
```

APR is the summed daily reward value over eligible TVL, valued at the pools' oracle TWAP (SPEC §6.6).
For `NFT_HOLD` (one share per NFT) it is the yield per NFT (daily rewards ÷ eligible NFTs, × 365) over
the collection's floor price; with no floor, the row gives the per-NFT daily reward instead. An
`NFT_HOLD` row also carries `collection: { did, collection_id }` when the target is a `col1` id.

### `GET /campaigns?creator=&target=&reward_asset=&status=` and `GET /campaigns/{id}`

Returns the struct from SPEC §3.1 in full (params expanded, including the excluded addresses,
`params.blacklist`, which every claim must reveal), plus:
- `fee` and `budget`;
- `created_height` and `distributor`;
- `emitted`, which is `emittable(peak)`;
- `posted_total` (in the active root) and `claimed`;
- `claim_window` (the creator's choice) and `reclaim_from` (`end + claim_window`);
- `reclaimable` (`budget − claimed`, from `reclaim_from`) and `reclaimed`. Until `reclaimed`, holders can
  still claim, even past `reclaim_from`.

### `GET /users/{address}/rewards?reward_asset=`

```json
{ "puzzle_hash": "…", "address": "xch1…",
  "rewards": [{
    "distributor": "…", "reward_asset": "…", "symbol": "…",
    "campaigns": [{
      "campaign_id": "…",
      "earned": "…",          // floor(accrued) from the engine, posted or not
      "cumulative": "…",      // in the active root
      "claimed": "…",         // from the claim slot
      "claimable": "…",       // cumulative − claimed
      "pending": "…",         // extra in a root still in its dispute window
      "qualifies": true,         // meets every gate right now
      "multiplier_bps": 15000,   // current boost (10000 = none), capped by the campaign
      "reclaim_from": 4850000,   // claims stay open past this until the creator reclaims
      "reclaimed": false,
      "proof": { "path": 5, "hashes": ["…"] } }] }] }
```

### `GET /roots/{distributor}/{root_id}`

The manifest exactly as pinned (see `flpcr/manifest.py`), with its `manifest_hash`, the `post_root`
spend's coin id and height, `activates_at`, and, while one is open, `dispute` (`disputer`,
`bond_parent`, the dispute spend's height); a disputed root does not activate at `activates_at`.

### `GET /links?address=`, `GET /links/log?distributor=` and `POST /links`

Address links (SPEC §6.8), one log per distributor.
- `POST` body: `{ distributor, primary, member, seq, unlink, member_pk, member_sig, primary_pk, primary_sig }`.
  A link carries both `signMessageByAddress` results over
  `flpcr-link-v2:<distributor_id>:<primary>:<member>:<seq>`; an unlink carries one, from either address,
  over `flpcr-unlink-v2:<distributor_id>:<primary>:<member>:<seq>`.
- The API verifies the signatures and addresses (`flpcr.links.verify`), refuses a `seq` not above the
  pair's last signed entry, appends the entry to the distributor's log at the current peak height, and
  returns it (the `flpcr.links.link_to_json` form).
- `GET /links` returns an address's group, the entries that formed it, and each pair's last `seq`.
- `GET /links/log` returns the whole log (`flpcr.links.log_to_json`). Every root manifest commits the
  prefix it used (`links.count`, `links.sha256`), so a watcher can check the log against the roots.

### `GET /distributors`

The registry contents: per reward asset, the launcher, the config, and a summary of current state.

## Write (keyless: the user's wallet signs every user spend; the resolve build is server-side)

### `POST /campaigns/quote`

Body: the campaign fields minus `distributor_id`, which is resolved from `reward_asset`. `claim_window`
(blocks) is required, from `3 × dispute_blocks + 32` (testnet 56, production 146) up to the longest
campaign. `start` must be at least 32 blocks after the current peak and no more than
`max_campaign_length` after it. The distributor's bounds are returned in the error if either is out of
range.
Optional `params.gates` and `params.boosts`, and `params.fee_window` (blocks, default 4608 = one day: the
fee-share bucket and lookback of SPEC §6.3):

```json
"params": {
  "gates":  [{ "holding": "NFT", "asset": "<minter DID launcher id>", "min_count": 1 }],
  "boosts": [{ "holding": "NFT", "asset": "<minter DID>", "min_count": 3, "bps": 3000 },
             { "holding": "CAT", "asset": "<asset id>", "min_count": 1000000, "bps": 2000 }],
  "boost_cap_bps": 25000,
  "fee_window": 4608
}
```

Returns:
- the validated struct and `campaign_id`;
- `fee` (3%, paid to the dev multisig in the create spend), `budget`, the per-day emission and `reclaim_from`;
- the **spend to sign**: one of the creator's own coins, at the creator's address, supplies `amount` of
  `reward_asset` and sends `"k" + campaign_id` to the distributor's current coin.

`kind` is `LP_HOLD`, `CAT_HOLD` or `NFT_HOLD`. An `NFT_HOLD` target is a minter DID (every NFT it
minted) or a collection given as `{ "did": "did:chia:…", "collection_id": "<metadata collection.id>" }`,
which the quote turns into its `col1` id (SPEC §6.8); `fees_bps` must be 0. `params.blacklist` lists up
to 16 addresses the campaign never pays (the burn address never earns anyway); the campaign id commits
to it.

Validation errors follow the same rules as the `create` leaf, so a quote that passes builds. The quote
also applies the router's rules (`flpcr/router.py`): the reward asset, and a `CAT_HOLD` target, must
trade in a Forge registry pool (or be XCH), an `LP_HOLD` target must be a registered pool, and an
`NFT_HOLD` target must be on `RouterRules.nft_collections` when that list is non-empty.

### `POST /campaigns/create`

Body: `{ quote_id, creator_spend }`: the creator's signed spend from the quote, which supplies the
deposit and sends `"k" + campaign_id`. The responder builds the `create` spend around it, adds the
**router's message** (signed by our router key, the only key our distributors accept), pushes the
bundle and returns its id. The `create` leaf needs both messages in one bundle; a "give `amount`,
request nothing" offer is not enough, because it carries no message from the creator. For a CAT reward,
any deposit above `amount` must come back as a CAT change output.

### `POST /claims/build`

Body: `{ address, campaigns?: [...], delegate? }`. With no `campaigns`, the build covers every claimable
campaign. Returns the unsigned bundle (claim actions, slot spends) and what must be signed. No
authorization names a generation of the distributor, so claims from many wallets can share one bundle:
- **Holder, standard wallet address:** a key signature (`AGG_SIG_UNSAFE`) over each claim leaf, by the
  synthetic key whose standard puzzle hash is `address`. The address needs no coin. A wallet signs the
  claims of all its addresses in one `chip0002_signCoinSpends` request for a never-pushed, zero-amount
  stand-in spend `(q . ((49 key leaf) ...))`, the pattern of Forge's fee proof. One of the wallet's
  coins (any address) pays the network fee in the same bundle.
- **Holder, any other puzzle (vault, multisig):** a spend of a coin **at `address`** that sends
  `"k" + leaf` (mode 16) for each claim, built and signed its own way.
- **Delegate (hot wallet for a cold wallet, cloud wallet, vault signer):** pass `delegate` with the
  delegate's address. The claim proves the holder's grant through the delegation slot, and the delegate
  authorizes the leaf either way above. The payout still goes to `address`.

Claims that cannot share one singleton spend (two claims touching any shared claim or delegation slot
or neighbour, or two payouts of the same amount to the same puzzle hash) go in **chained generations
of the same bundle** (`Distributor.sequence`); what the cost budget does not fit comes back for a
later bundle. A replayed signature or message pays nothing (the slot records `claimed`), and whoever
submits a claim cannot change where it pays. Each claim action carries the campaign's excluded list
(from the campaign record); the leaf refuses the burn address and any listed address.

### `POST /delegates/build` and `GET /delegates?holder=`

Body: `{ holder, delegate, grant: true | false }`. Returns the `delegate` spend and what the **holder**
must authorize: a signature over `sha256tree(["flpcr-delegate-v2", distributor_id, holder, delegate,
grant])` for a standard address, or that message (prefixed `"k"`) for a vault or multisig to send. The
bundle must also leave the distributor's `delegate_fee` as network fee (testnet 1,000,000 mojos,
production 50,000,000), on every grant and every revoke. A grant is per distributor, i.e. per reward
asset. Granting twice and revoking once leaves the delegate revoked; revoking a pair that was never
granted is refused. `GET` lists a holder's delegates. Delegation slots are hinted with their key,
`sha256tree((holder . delegate))`, not with the holder, so the API looks a known pair up by that hint
and lists a holder's delegates from its index of `delegate` spends.

### `POST /reclaims/build`

Body: `{ campaign_id, delegate?, admin? }`. This is refused before `reclaim_from`. Returns the bundle that
pays the creator and what must authorize it, over `sha256tree(["flpcr-reclaim-v1", distributor_id,
campaign_id])`:
- **the creator:** a signature, or that message (prefixed `"k"`) for a vault or multisig to send;
- **the creator's granted delegate:** the same, from the delegate;
- **the admin** (`admin: true`, the dev multisig): that message (prefixed `"k"`) from a coin at
  `admin_ph`.

The payment goes to the creator whoever authorizes it. A bundle without one of these is refused on
chain.

### `POST /disputes/build`

Body: `{ distributor, root_id, disputer, coin }`. Anyone may dispute a pending root; the puzzle asks for
no authorization, the bond is the authorization (Merkl's dispute with the stake as a coin). `disputer` is
the address a refund goes to, the disputer's own choice; `coin` is one of the disputer's XCH coins
holding at least `dispute_bond` plus the network fee. Refused if the root is not pending (a root past
its window is promoted by the next spend) or a dispute is already open (one at a time). Returns the
unsigned bundle and the **spend to sign**:
- **the disputer:** a spend of `coin` that creates the **bond launcher**, a coin at the distributor's
  `bond_launcher_hash` holding exactly `dispute_bond` mojos of XCH (production 5 XCH =
  5,000,000,000,000, testnet 0.1 TXCH = 100,000,000,000), hinted with the distributor id, with the rest
  as change. A standard wallet signs it with `chip0002_signCoinSpends`. The bond is XCH whatever the
  reward asset.

The bundle already carries the launcher's spend, which creates the bond coin at the distributor's
`bond_puzzle_hash` and announces it, and the `dispute` action, which asserts that announcement. A bond
that was never funded, or funded with the wrong amount, is refused on chain. Once confirmed, the root
cannot activate until it is resolved; `GET /roots/{distributor}/{root_id}` shows `dispute`
(`disputer`, `bond_parent`, the dispute spend's height).

### `POST /disputes/resolve/build`

Body: `{ distributor, upheld: true | false, admin? }`. Server-side only: the sender must be the
**guardian** (default) or the **admin** (`admin: true`, the dev multisig). Refused if no dispute is open.
Returns the bundle and what must authorize it: a coin at `guardian_ph` or `admin_ph` sends
`"s" + sha256tree([root_id, upheld])` (mode 23) to the distributor's current coin. The bundle also spends
the **bond coin** with the one delegated puzzle the singleton names (`[51, recipient, dispute_bond]`,
hinted); any other puzzle is refused on chain.
- **upheld:** the pending root is revoked (the operator reposts a corrected root under the same id) and
  the bond is refunded to the disputer's address.
- **rejected:** the root stays pending with its window restarted (`activates_at = h + 32 +
  dispute_blocks`) and the bond is forfeited to the dev multisig (`dev_fee_ph`).

Neither resolver can send the bond anywhere else. While a dispute is open, `POST /claims/build` still
builds against the previous active root, and `pending` in `GET /users/{address}/rewards` stays pending.

## Rate limits and keys

Anonymous requests are limited to 10 req/s (Merkl's number); an `X-API-Key` header lifts the limit. The
read side is a cache of chain data plus manifests, so anything it serves can be recomputed from chain.
