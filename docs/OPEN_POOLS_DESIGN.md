# NFT campaigns and open reward pools: design

> **Status update (2026-10-20, later).** The owner decided that the open pool (Part 2, `balance / 365`
> per day) is a **separate puzzle, for later**, not part of the campaign distributor; LP and NFT
> Merkl-style campaigns come first. **Part 1 is built** on the campaign distributor: `Kind.NFT_HOLD`,
> targeting a minter DID or a `col1` collection (DID + metadata collection id, checked against the
> on-chain metadata hash), proven on testnet11 by the fifth distributor (`docs/TESTNET.md` §5). The
> excluded addresses and "every holder in every root" decisions below are built too (SPEC §5.4, §6.1).
> The rest of this page is kept as the design for the open pool.

Status: design, 2026-10-20. Owner decisions are in the first table; everything else is a proposal
to build against.

## Owner decisions (2026-10-20)

| Question | Decision |
|---|---|
| Who is in a root | Every holder, whether or not they ever claim. Whatever is not claimed by the end of a campaign's window goes back to the creator |
| The "claim costs a transaction" rule | Met by the claim bundle's one fee-paying spend: the user pays the network fee. No coin per address |
| Excluded addresses | The burn address (`0x…dead`) can never claim. A creator can list up to 16 more at launch; the claim leaf refuses them |
| NFT campaigns | A campaign can reward the holders of an NFT collection instead of an LP |
| Open pool: daily amount | `balance / 365` per day, **recomputed from the current balance**. Top-ups raise the daily amount and lengthen the pool; it tapers and ends at a dust floor |
| Open pool: NFT and LP together | The creator sets a percent split at launch (e.g. 60% to NFT holders, 40% to LP holders), committed on chain |
| Open pool: assets | **Any asset sent to it**: XCH and any CAT, each paid out at its own `balance / 365` per day |

## Part 1: NFT campaigns (`Kind.NFT_HOLD = 3`)

A third campaign kind beside `LP_HOLD` and `CAT_HOLD`. Unlike CHIP-0051's reward distributor,
nothing is staked: the NFTs stay in the holders' wallets, tradable and listable, and the engine
scores who held them, as it does for LP.

- **Target** is the collection: the launcher id of the DID that minted it. On chain, that DID is the
  only thing that ties NFTs together (a "collection id" in metadata is off-chain and can be copied).
- **Weight** is one share per NFT, time-weighted per block like LP. A holder with three NFTs for the
  whole interval earns three times a holder with one. A flash buy held a block or two earns next to
  nothing.
- **Holder** is the owner's inner puzzle hash under the NFT ownership layer (the address the wallet
  shows), the same key space as LP holders, so one wallet claims both in one bundle.
- **Not a position:** the burn address, the creator's excluded list, and NFTs parked in an offer
  settlement mid-trade (`OFFER_MOD`). An NFT listed in an open offer stays in the owner's wallet and
  still counts.
- **On chain:** `create` accepts `kind == 3` with `fees_bps == 0` (an NFT has no trading fee to
  weight by). Nothing else in the leaves changes: claims, roots and the emission ceiling are per
  campaign, not per kind.
- **Live source:** walk the DID's singleton lineage; every NFT launcher a DID spend created is in the
  collection. Then follow each NFT singleton forward coin by coin (`Puzzle.parse_child_nft`), reading
  its owner at each transfer. Every NFT coin becomes +1 for its owner at creation and −1 at its spend,
  the same events the LP crawl produces, so the engine needs only `Source.nft_events`, which it
  already reads for gates and boosts.

## Part 2: open reward pools

A new distributor shape, one singleton per pool. A pool has no end date: it pays out what it holds.

### What a user sees

- One **deposit address**. Anyone sends XCH or any CAT to it, at any time, from any wallet. Those
  coins are the pool's.
- Each asset in the pool pays out `balance / 365` a day, recomputed as the balance changes. A deposit
  raises tomorrow's daily amount and makes the pool last longer.
- The daily amount goes to the pool's participants: holders of an NFT collection, of a Forge LP, or
  both at the creator's split. Within each group, shares are time-weighted as in a campaign.
- Holders claim every asset they have earned, across every address in their wallet, in one approval
  and one bundle (the sequencer already does this).

### On chain

**Deposit address.** `p2_delegated_by_singleton(pool launcher, nonce 0)`: a puzzle only the pool
singleton can spend. XCH sent to it sits at that puzzle hash; a CAT sent to it sits at
`CAT(asset, that puzzle hash)`, which every wallet already does when you paste the address.

**Absorb.** A new leaf folds deposit coins into the pool. Anyone can run it (a keeper does so before
each root). It spends the deposit coins by message and adds their sum to that asset's `balance` in
state. Topping up an asset the pool already holds is permissionless. A **new asset** needs the
router's message, as `create` does, because state holds one row per asset and the number of rows is
capped (16). Without that gate, anyone could fill every row with a worthless CAT for a few mojos.
Coins of an unapproved asset just sit at the deposit address, harmless.

**Per-asset reserves.** Each asset's funds sit in their own reserve coin, a
`p2_delegated_by_singleton` coin (nonce derived from the asset id), XCH or wrapped in that CAT. A
leaf moves an asset's funds by sending its reserve a message with the delegated puzzle's hash. That
is the pattern the dispute bond already uses, and it pays several assets in one spend. The upstream
reserve finalizer handles one reserve, so this replaces it for open pools.

**State.** `assets: [(asset_id, balance, posted, claimed)]`, sorted by asset id, plus the root,
dispute and rotation fields every distributor has. `balance` is what the reserve holds. `posted` is
the sum the roots have released for that asset. `claimed` is what has been paid out of it. What
is still unallocated is `balance − (posted − claimed)`.

**The emission ceiling (the on-chain guarantee).** A root over `[start, end)` may raise each
asset's `posted` by at most

```
unallocated × (end − start) / (365 × 4608)          with end − start ≤ 4608 (one day)
```

This is `balance / 365` a day, recomputed at every root, as the owner chose. Over many roots the
amount tapers geometrically. Holding each root to at most a day keeps the linear step within 1/365²
of a true daily recompute. Below a dust floor the step rounds to zero and the pool is spent.

**Participants and the split.** Curried into the pool's Config at launch:
`participants: [(kind, target, bps)]`, for example `[(NFT_HOLD, did, 6000), (LP_HOLD, pool, 4000)]`,
with the bps summing to 10,000. The leaves never read the split. It is committed by the Config (so
by the pool's puzzle hash), and the engine applies it. A wrong split is a dispute, the same as a
wrong share in a campaign.

**Claims.** One leaf per `(asset, holder)`:
`["flpop-claim-v1", pool_id, asset_id, puzzle_hash, cumulative]`. A claim slot per
`(asset, holder)` records what has been paid. The same generation-free authorization (key signature
or mode-16 message), the same burn-address and excluded-list refusals, and the same sequencer.

**Unchanged from campaigns:** `post_root` (per-asset totals instead of per-campaign), the dispute
window, `dispute`/`resolve` with a bond, `veto`, `rotate`. There is no `create` (the pool is one
open-ended campaign) and no `reclaim` (nothing ends).

### The engine

- It reads the pool's absorb history from chain (each absorb's spend states the new balance), so it
  knows each asset's unallocated amount at each root's start. It emits exactly the ceiling: no more is
  allowed, and less would strand funds.
- For each interval it splits each asset's emission by the participants' bps, then within each group
  by time-weighted share (NFT count or LP share).
- A group with no eligible holder in an interval allocates nothing; that part stays in the pool.

### Open questions for the owner

1. **Unclaimed rewards in a pool that never ends.** A campaign returns them to its creator at the end
   of its window. A pool has no end, so what is posted and never claimed stays locked forever. Two
   options:
   - claims expire a set number of days after they are posted and the amount flows back into the
     pool;
   - nothing expires.

   Expiry needs per-root bookkeeping and makes the claim leaf more expensive.
2. **Who may launch a pool, and is there a creation fee?** For campaigns, the router signs creation
   and the dev fee (3%) is taken from the deposit. For pools there is no single deposit to take it
   from.
3. **Can anyone close a pool?** As designed, no one can withdraw: it only pays holders. An admin
   close-out (return everything unallocated to the creator) is possible but changes what a depositor
   is trusting.

## Phases

| Phase | Scope | Size |
|---|---|---|
| A | `NFT_HOLD` campaigns: create leaf accepts kind 3, engine scores NFT counts, live NFT crawl of a collection, tests and a testnet campaign | Small: no new leaves |
| B | Open pool with XCH only: deposit address, absorb leaf, per-day ceiling, per-asset claims (one asset), participants split | Medium: new distributor shape |
| C | Any asset: per-asset reserve coins, router-approved new assets, CAT reserves, 16-asset cap | Large: replaces the reserve finalizer |
| D | Front end: pool page (assets, daily amount per asset, participants), deposit address with a copy button, claim every asset in one approval | Medium |
