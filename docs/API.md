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
{ "target": "<pool launcher | cat asset id>", "kind": "LP_HOLD", "name": "TXCH/wUSDC",
  "tvl_xch": "1234500000000", "apr_bps": 2310,
  "daily_rewards": [{ "asset": "<id>", "symbol": "wUSDC", "amount": "41000000" }],
  "campaigns": ["<campaign id>", "..."] }
```

APR is the summed daily reward value over eligible TVL, valued at the pools' oracle TWAP (SPEC §6.6).

### `GET /campaigns?creator=&target=&reward_asset=&status=` and `GET /campaigns/{id}`

Returns the struct from SPEC §3.1 in full (params expanded), plus:
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
spend's coin id and height, and `activates_at`.

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

## Write (keyless, Sage offers)

### `POST /campaigns/quote`

Body: the campaign fields minus `distributor_id`, which is resolved from `reward_asset`. `claim_window`
(blocks) is required; the distributor's bounds are returned in the error if it is out of range.
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
- the **offer to make**: give `amount` of `reward_asset`, request nothing.

Validation errors follow the same rules as the `create` leaf, so a quote that passes builds. The quote
also applies the router's rules (`flpcr/router.py`): the reward asset, and a `CAT_HOLD` target, must
trade in a Forge registry pool (or be XCH), and an `LP_HOLD` target must be a registered pool.

### `POST /campaigns/create`

Body: `{ quote_id, offer }`. The responder uses the offer's settlement coin as the `create` leaf's input,
adds the **router's message** (signed by our router key, the only key our distributors accept), takes
the network fee from the offer, pushes the bundle and returns its id.

### `POST /claims/build`

Body: `{ address, campaigns?: [...] }`. With no `campaigns`, the build covers every claimable campaign.
Returns the unsigned bundle (claim actions, slot spends) and what the holder must sign. Only the holder
can claim:
- **Standard address:** the claim names the address's synthetic public key; the holder signs the
  `AGG_SIG_ME` with `chip0002_signCoinSpends` and pays the network fee from any of their coins.
- **Vault or other puzzle:** the response includes the `"k" + leaf` message the holder's own puzzle must
  send; their wallet (e.g. the multisig safe) builds and signs that spend.

- **Delegate (hot wallet for a cold wallet, cloud wallet, vault signer):** pass `delegate` with the
  delegate's address. The claim names the delegate; the delegate signs (or its puzzle sends the message).
  The payout still goes to `address`.

A bundle without the holder's or a granted delegate's authorization is refused on chain.

### `POST /delegates/build` and `GET /delegates?holder=`

Body: `{ holder, delegate, grant: true | false }`. Returns the `delegate` spend and what the **holder**
must authorize: a signature over `sha256tree(["flpcr-delegate-v1", distributor_id, holder, delegate,
grant])` for a standard address, or that message (prefixed `"k"`) for a vault or multisig to send. A
grant is per distributor, i.e. per reward asset. `GET` lists a holder's live delegates (their slots are
hinted with the holder).

### `POST /reclaims/build`

Body: `{ campaign_id, delegate? }`. This is refused before `reclaim_from`. Returns the bundle that pays
the creator and what the **creator** (or the creator's granted delegate) must authorize: a signature over
`sha256tree(["flpcr-reclaim-v1", distributor_id, campaign_id])`, or that message for a vault or
multisig to send. A bundle without it is refused on chain.

## Rate limits and keys

Anonymous requests are limited to 10 req/s (Merkl's number); an `X-API-Key` header lifts the limit. The
read side is a cache of chain data plus manifests, so anything it serves can be recomputed from chain.
