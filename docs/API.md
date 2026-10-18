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
- `claim_deadline` (`end + claim_window`);
- `reclaimable` (`budget − claimed`, once past the deadline) and `reclaimed`.

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
      "claim_deadline": 4850000,
      "proof": { "path": 5, "hashes": ["…"] } }] }] }
```

### `GET /roots/{distributor}/{root_id}`

The manifest exactly as pinned (see `flpcr/manifest.py`), with its `manifest_hash`, the `post_root`
spend's coin id and height, and `activates_at`.

### `GET /distributors`

The registry contents: per reward asset, the launcher, the config, and a summary of current state.

## Write (keyless, Sage offers)

### `POST /campaigns/quote`

Body: the campaign fields minus `distributor_id`, which is resolved from `reward_asset`.

Returns:
- the validated struct and `campaign_id`;
- `fee` (3%), `budget`, the per-day emission and `claim_deadline`;
- the **offer to make**: give `amount` of `reward_asset`, request nothing.

Validation errors follow the same rules as the `create` leaf, so a quote that passes builds.

### `POST /campaigns/create`

Body: `{ quote_id, offer }`. The responder uses the offer's settlement coin as the `create` leaf's input,
takes the network fee from the offer, pushes the bundle and returns its id.

### `POST /claims/build`

Body: `{ address, campaigns?: [...] }`. With no `campaigns`, the build covers every claimable campaign.
Returns a bundle with the claim actions and slot spends, and the offer the holder must make to pay the
network fee (give `fee` XCH, request nothing). Anyone may submit for anyone; payouts always go to the
holder.

### `POST /reclaims/build`

Body: `{ campaign_id }`. This is refused before `claim_deadline`. Returns the bundle that pays the
creator.

## Rate limits and keys

Anonymous requests are limited to 10 req/s (Merkl's number); an `X-API-Key` header lifts the limit. The
read side is a cache of chain data plus manifests, so anything it serves can be recomputed from chain.
