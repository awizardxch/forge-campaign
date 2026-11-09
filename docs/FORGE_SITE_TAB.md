# FLPCR on forge.awizard.dev: the 🪎 tab

> **Status:** plan, 2026-11-09. This is the integration of FLPCR into the Forge site (`awizardxch/forge-ui`). It
> is written from forge-ui `9cd0b2b` and forge-campaign `main`. Nothing here is built in forge-ui yet. The
> service side (`flpcr/service/`) exists, except for the create intake (§5), which is the next thing to build
> in this repo.

## 1. Owner decisions this plan follows

- **The tab.** Campaigns get their own tab, labelled with the treasure chest emoji 🪎 (U+1FA8E). It sits
  beside 🔐 and 🪄⚡️, which are also emoji-only tabs.
- **Pools.** Only Forge V16 pools and newer are supported (CLAUDE.md, 2026-11-09). The tab never lists a
  V15 or V14 pool as a target.
- **Signing.** Every signature comes from the user's wallet over WalletConnect (`chip0002_*`), as in the
  rest of forge-ui. The site holds no key. The service holds only the operator key; the router key lives
  with the create intake (§5).

## 2. The emoji

🪎 arrived in Emoji 17.0 (2025). Devices whose emoji font predates it draw an empty box: older iOS, Android
and Windows builds.

- **Fallback:** detect the glyph once at boot, by measuring it against a known-missing codepoint on a
  canvas. Where it is missing, render a 16 px treasure chest image in its place.
- **Image:** forge-ui already ships pixel treasure chest art (`pixel-treasure-chest-glow` in
  `src/lib/lpPositionMetadata.ts`).
- **Tab text:** keep the literal `🪎` in `MAIN_TABS`, so the label and the route stay text, and swap the
  rendering only.

## 3. Where it goes in forge-ui

`src/App.tsx`:

```ts
const TAB_TO_PATH = { ..., campaigns: '/campaigns' };
const MAIN_TABS = [ ..., ['multisig', '🔐'], ['spellpower', '🪄⚡️'], ['campaigns', '🪎'], ['markets', 'Markets'], ... ];
```

- **Visibility:**
  - On testnet11, the tab is on the strip for everyone.
  - On mainnet, it stays in `HIDDEN_TABS` until the mainnet gate (§8). The route still resolves, so
    `/campaigns` can be tested by typing it.
- **New files** (forge-ui conventions: one panel, one `lib/` module, a `__checks__` file, a doc):

| File | Holds |
|---|---|
| `src/components/CampaignsPanel.tsx` | the three views (§4) |
| `src/lib/campaigns/api.ts` | typed client for the service (§6) |
| `src/lib/campaigns/claimProof.ts` | the claim stand-in spend (§4.3); reuses `relay/clvm` like `feeProof.ts` |
| `src/lib/campaigns/leaf.ts` | `claim_leaf` and the campaign id in TS, so the page can check what the service hands it |
| `src/lib/__checks__/campaignVectors.check.ts` | the TS mirrors against `fixtures/vectors.json` published by forge-campaign (§7) |
| `docs/FORGE_CAMPAIGNS.md` | the tab's own doc, like the other `docs/FORGE_*.md` |

## 4. Views

### 4.1 Campaigns (anyone)

A list from `GET /api/v1/status` with one row per campaign. Each row shows:
- the target: pool name for `LP_HOLD`, NFT collection for `NFT_HOLD`;
- the reward asset and budget;
- the daily emission: budget ÷ days, equal every day;
- the phase: scheduled / running / claim window / reclaimable;
- the time left;
- the APR.

APR is daily reward value over eligible TVL. TVL and prices come from the routers forge-ui already calls
(`api.forge.awizard.dev`, `testnet-api.forge.awizard.dev`). An NFT campaign shows yield per NFT, plus an
APR over the floor price when Dexie lists one, as `scripts/dashboard.py` does today.

**Campaign page:**
- the rules, as committed in the campaign id;
- excluded addresses;
- the roots posted, with the pending root and its `activates_at`;
- any open dispute;
- a link to the distributor on Spacescan.

### 4.2 My rewards (connected wallet)

For every address the wallet scan already holds (forge-ui scans the wallet's addresses at connect), call
`GET /api/v1/claimable/<address>`. Then:

- **Sum per campaign:**
  - claimable now;
  - claimed;
  - pending, in a root that is still in its dispute window.
- **Not yet posted:** "earned" shows nothing until the service exposes the engine's running total.
- **Proof gaps:** if the service reports `proof_gap`, show the message, and show no Claim button for roots
  past the gap. The service never hands out a proof the chain would refuse.

### 4.3 Claim all (one approval, one fee)

This is the flow `scripts/deploy_testnet.py claim-all` proved on testnet11, moved into the page:

1. **Collect the claims:** `(address, synthetic public key, leaf)` for every claimable leaf. The scan has
   each address's synthetic key.
2. **Build the stand-in spend.** One zero-amount coin whose puzzle is `(q . ((49 pk leaf) ...))`, one
   condition per claim. Its parent is `sha256("flpcr-claim-proof")`, the same as `Wallet.sign_claims`. It
   is never pushed, so the signature authorizes those claims and nothing else. `feeProof.ts`'s
   `keysProofSpend` is the same shape with one message for every key; here each key signs its own leaves.
3. **Build the fee spend.** One standard spend of one of the wallet's XCH coins:
   - `CREATE_COIN` for the change back to itself;
   - `RESERVE_FEE` of at least what the service quotes for the batch. Under 5 mojos per unit of cost, the
     mempool refuses a nonzero fee.
4. **Sign.** One `chip0002_signCoinSpends` request with both spends, so the wallet shows a single prompt.
5. **Submit.** `POST /api/v1/claims` with:
   - `{claims: [{campaign, address, public_key}], signature, spend_bundle}`;
   - `signature` is the stand-in's part of the aggregate;
   - `spend_bundle` is the fee spend with its own signature.

   Split the aggregate the way `nodePushFallback` does. Alternatively, sign the two in two requests if a
   wallet will not split; that costs one more prompt.
6. **Track.** Poll `GET /api/v1/claims/<id>` until confirmed. The sequencer batches many claimants per
   bundle, so a claim can wait up to one batch interval.

- **Lock and multisig holders** (🔐 tab): a lock claims by a mode-16 message `"k" + leaf` from its own
  address in an executed proposal (`docs/TESTNET.md` §6). The lock tab needs a "claim campaign rewards"
  proposal type that builds that message and submits the executed proposal's bundle to the same
  endpoint.

### 4.4 Create (creator)

The form takes:
- target: a V16 pool from the registry, or an NFT collection (DID or `col1`);
- reward asset: XCH, or a CAT in a V16 pool;
- total amount and length in days;
- claim window;
- optional excluded addresses (at most 16).

The page shows:
- the 3% dev fee;
- the budget;
- the daily emission;
- the date leftovers can be reclaimed.

**Submitting** goes through the create intake (§5): quote, sign the creator's spend, submit.

### 4.5 Reclaim and delegate (creator, holder)

- **Reclaim:** the creator reclaims after `end + claim_window`. The page applies the same guard as
  `reclaim_blockers()`: no button while the final root is pending, or while it has been claimable for less
  than a window and holders still have unclaimed amounts.
- **Delegate:** grant and revoke, each costing `delegate_fee`. This can wait until after launch: it serves
  cold wallets and vaults, not first users.

## 5. What forge-campaign must build first: the create intake

Creation needs two parties on chain:
- our router's message `"c" + id`;
- the creator's own coin spend, which sends `"k" + id` to the singleton (mode 23) and supplies the
  deposit.

**The constraint:** mode 23 names the singleton's current coin id. A creator's signed spend therefore goes
stale as soon as anything else spends the singleton: a claim batch, a root, another create.

**Proposed intake,** a new module beside the sequencer, sharing the service's single-writer loop:

- **`POST /api/v1/campaigns/quote`.** Runs `router.check_campaign` and the `RouterRules`, and computes the
  campaign id, fee, budget and daily emission. It returns the exact conditions the creator's spend must
  carry: the deposit, and the `"k" + id` message to the named singleton coin. It also returns a **lease**:
  the intake holds the next singleton generation for this create for N seconds (proposed: 120).
- **`POST /api/v1/campaigns/create`.** Takes the creator's signed spend bundle and checks it, the way the
  sequencer checks a claim: deposit, message, signature, fee. It adds the router's message spend and
  pushes before any other action spends the singleton.
- **Lapsed lease:** the page re-quotes, and the wallet signs again.

**The alternative removes the lease:** make the creator's consent generation-free, as claims are (mode 16,
bound by the campaign id). That is a leaf change, so it means a re-pin and a review item. The intake design
above needs no puzzle change, so it is the default.

**Where the router key lives:** with the intake, as a second key on the host, separate from the operator
key. The status artifact's "server holds the operator key only" then becomes "operator and router keys".
**Owner decision:** keep the router key on the same host, or a separate signer.

## 6. Service API the tab uses

These are the paths that exist today (`flpcr/service/http.py`), all JSON and CORS-open:

| Method and path | Use |
|---|---|
| `GET /health` | up, and the peak |
| `GET /api/v1/status` | distributor, campaigns, active and pending roots, dispute, rotation, proof gap |
| `GET /api/v1/claimable/<address>` | a holder's leaves in the claimable root, with leaf and proof |
| `POST /api/v1/claims` | the sequencer: signed claims |
| `GET /api/v1/claims/<id>` | progress of a submitted claim |

**To add for the tab:**
- `POST /api/v1/campaigns/quote` and `POST /api/v1/campaigns/create` (§5);
- `GET /api/v1/campaigns/<id>`, the campaign page in one call, with the engine's running total as
  "earned".

**Host (proposal):** behind the Hetzner proxy that already serves the routers. Either
`api.forge.awizard.dev/flpcr/...` and `testnet-api.forge.awizard.dev/flpcr/...`, or a `campaigns-api`
subdomain per network. The site reads the base URL from `networkConfig.ts`.

## 7. Keeping the TS mirrors honest

The page recomputes each leaf and campaign id before asking for a signature. A wallet should never be
asked to sign a leaf the page cannot reproduce. `fixtures/vectors.json` in forge-campaign holds:
- claim leaves;
- campaign ids;
- the stand-in spend's puzzle hash.

`scripts/make_vectors.py` regenerates it, and `tests/test_vectors.py` fails when it is stale. forge-ui
copies it into its checks, as it does for its other quoting mirrors.

## 8. Mainnet gate for the tab

The tab goes on the mainnet strip only when all of these hold:

1. a mainnet distributor exists on the current pins, with the guardian as a Forge multisig safe and the
   dev multisig as `dev_fee_ph` and `admin_ph`;
2. the external review is done (SPEC §7);
3. the service and the watcher run on Hetzner against it;
4. one small campaign of our own has run end to end there.
