# Rerun of the 2026-10-19 review probes against the ten-leaf build

> **Date:** 2026-11-09
> **Revision:** `3b4600c` (`main`), ten leaves plus `bond_launcher`.
> **Compiled fingerprint:** `(cd contracts/compiled && sha256sum *.hex | sha256sum)` =
> `bb2781ffef81d2a59c132e5b5b669f6fecdae8c3f8e93db3d67f847897a93146`; `python contracts/build.py --check` clean.
> **Suite at this revision:** 392 passed, 5 skipped.
> **Fix rerun (branch `fix-bond-reclaim`, 2026-11-09):** compiled fingerprint
> `c730114474886304d045a2b8db807f10adf992e568d8231fbe1e7f53e0e7a422`; 461 passed, 5 skipped; `run_all.py` all
> ok. The outputs in `output/` and the cost table are from this rerun.

The original probes in `../probes/` are the historical record and are left as they were: they target the
eight-leaf build of `dea1d7d` and its `Distributor.spend` API, which no longer exist. Each script here ports
one finding's attack to the current driver (`LeafCall` / `prepare` / `apply`, `flpcr/chain/sim.py`), next to
an honest control in the same world, for XCH and CAT rewards.

```bash
python reviews/2026-10-19/rerun/run_all.py      # every script, one line each (~2 min)
PYTHONPATH=.:reviews/2026-10-19/rerun python reviews/2026-10-19/rerun/r02_ungranted_delegate.py
python reviews/2026-10-19/rerun/cost.py          # the cost table below
```

`r05_height_pin.py` compiles two mutants and needs `rue` 0.8.4 on `PATH`. Every script prints one line per
attempt, `[PASS]` when an attack was refused for its pinned reason or a control was accepted, and exits
non-zero otherwise. `output/` holds the run recorded for this README.

**How a refusal is pinned.** A consensus refusal names its rule, and the script requires that name
(`MessageNotSentOrReceived`, `AssertHeightAbsoluteFailed`, `DoubleSpend`, ...). A puzzle refusal is always
the same `clvm raise` (the driver runs the leaf locally first); each script's docstring says which
assertion is the first that can fire and why, and `scripts/mutate.py` is what pins it.

## Verdicts

| Finding | Script | Result | Evidence (current build; old result in brackets) |
|---|---|---|---|
| FLPCR-01 | `r01_09_17_engine.py` | PASS | flash deposit + dust swap: attacker 99.0 of 100,000 [49,549.5]; vs an honest swap 74.25 [24,799.5]; quiet control 100,000 |
| FLPCR-02 | `r02_ungranted_delegate.py` | PASS | ungranted claim + ephemeral spend of the slot the spend creates: `MessageNotSentOrReceived` [ACCEPTED]; same for a stranger's reclaim [ACCEPTED, 970,000 to creator]; granted delegate controls accepted |
| FLPCR-03 | `r03_dispute_window.py` | PASS | lagged `h = P − 31`: first claim at P+10 with D=8 [P+1], P+40 with D=38 [P+7] |
| FLPCR-04 | `r04_table_fill.py` | PASS (fixed differently) | table still fills, but the admin reclaims any row from `end + claim_window` (paid to its creator); stranger / fake admin `MessageNotSentOrReceived`; start beyond the horizon `clvm raise`; a fully claimed row removed paying 0; honest create accepted after |
| FLPCR-05 | `r05_height_pin.py` | PASS | future `h`: `AssertHeightAbsoluteFailed`; stale `h`: `AssertBeforeHeightAbsoluteFailed`; each mutant without its guard ACCEPTS the probe and `TestHeightPin` fails on it (4 and 2 failures) [92 passed] |
| FLPCR-06 | `r06_guardian_rotation.py` | PASS | `rotate(self) + post_root` in one spend: `MessageNotSentOrReceived` [ACCEPTED, 970,000 to the guardian]; pending rotation visible; admin cancels |
| FLPCR-07 | `r07_create_creator.py` | PASS | someone else names the creator: `MessageNotSentOrReceived` [ACCEPTED]; consent by signature `clvm raise`; 0/31/33-byte creator `clvm raise` [ACCEPTED, budget locked]; 31/33-byte rotate key and delegate owner `clvm raise` |
| FLPCR-08 | `r08_final_root.py` | PASS (`fix-bond-reclaim`) | floor `4 × (D + 32)`: D=8 window 160, D=38 window 280; final root at end+50..120 / end+110..210, holders keep at least one full cycle (D + 32) after it activates, alice claims, the creator reclaims the rest at the opening; a slow operator's final root posted the block before the opening blocks the reclaim (`clvm raise`, NEW-3). [6b8c027, floor `3D + 32`: D=8 (window 56) final root at end+79..120, the creator reclaimed 970,000 while it was pending; D=38 (window 146) failed with a late veto (end+178 / end+210)] |
| FLPCR-09 | `r01_09_17_engine.py` | PASS | one root vs roots cut at 998: identical cumulatives [50,450/49,549 vs 99,851/148] |
| FLPCR-10 | `r10_nft_reader.py` | PASS | forged NFT under a DID payment not counted [counted]; DID-authorized mint from an XCH coin counted [missed]; an unfollowable spend ends the holding |
| FLPCR-11 | `r11_links.py` | PASS | replayed link ignored [`{atk: 250}`]; primary unlinks; third-party unlink ignored; other distributor's link inert; backdated entry refused by the log; dropped committed entry refused; `from_json` keeps links |
| FLPCR-12 | `r12_delegate_fee_race.py` | PASS (race itself not modelled) | delegate with no fee or 1 mojo short: `ReserveFeeConditionFailed` [free]; a prebuilt singleton bundle still loses to a paid spend (`DoubleSpend`), a claim message is generation-free and survives |
| FLPCR-13 | `r13_batches.py` | PASS | every colliding batch refused by `BundleCollision` in the driver; with the check off consensus says `DoubleSpend` / `DuplicateOutput`, as the check anticipates; split or sequenced forms accepted |
| FLPCR-14 | `r14_double_grant.py` | PASS | grant, grant, revoke: delegated claim `MessageNotSentOrReceived` [ACCEPTED, 485,000]; one slot for the pair; a second slot for the key `clvm raise`; revoke + claim in one spend `BundleCollision` / `DoubleSpend` |
| FLPCR-15 | `r15_retro_start.py` | PASS | `h = P − 31, start = h` `clvm raise` [ACCEPTED]; `start = h + 31` refused; `start = h + 32` accepted and starts after its creating block |
| FLPCR-16 | `r16_refusal_hygiene.py` (+ `r05`) | PASS | second first-claim insert now `MessageNotSentOrReceived` [CoinAmountNegative / posted total]; 125 of 135 `refused()` calls pin `expect=`; a Python error is not a refusal |
| FLPCR-17 | `r01_09_17_engine.py` | PASS | float weights `TypeError` at the Source boundary and in `accrue`; rationals exact, `state_to_json` works |
| D-6 late veto | `r03_dispute_window.py` | still present (documented, SPEC §5) | guardian vetoes 10 blocks after `activates_at` with `h` lagged 31, singleton untouched since |
| E-7 wrapper | `r01_09_17_engine.py` | PASS | blacklisted vault: nothing forwarded to its holders |
| C-3 CAT excess | `r18_informational.py` | unchanged by design | excess without change: `AssertCoinAnnouncementFailed`; with a change output accepted |
| B-2 decoy slots | `r18_informational.py` | PASS | decoy at a live slot's puzzle hash: `AssertMyParentIdFailed`; resync ignores it |
| NFT melt | `r10_nft_reader.py` | PASS (stand-in) | the SDK helpers cannot build a melt on the simulator; an unfollowable spend ends the holding |

The verified-safe batteries (`A/p4_safe_battery.py`, `B/probe_slots.py`, `C/probe_c*.py`) were not ported
one by one; their cases are in `tests/test_chain.py` (`TestAssertionProbes`, `TestSpentSlotsStaySpent`,
`TestOnlyTheHolderClaims`, `TestClaimRefusals`, `TestCreateRefusals`, the CAT twins).

## Attacks that got through on 6b8c027 (fixed on `fix-bond-reclaim`)

`n_new_surface.py` probes what was added after the review (the delegation slot list, generation-free
signature claims, exclusions, the bond and dispute leaves). Sections A and B held; section C did not.
All four below are fixed on `fix-bond-reclaim` (re-pin of `bond_launcher`, `dispute`, `reclaim`,
`create`), and `n_new_surface.py` now ends "62/62 as expected": C5 `WrongPuzzleHash` (a launcher paying
the thief at the funded coin) and `MessageNotSentOrReceived` (the lifted launcher spent alone), C6
`MessageNotSentOrReceived`, C7 `clvm raise` during the dispute and during the restarted window, then the
honest claim and reclaim accepted. The record of what was found:

1. **NEW-1 (High): anyone who sees a dispute bundle steals the bond.** `bond_launcher.rue` is a plain
   puzzle anyone can spend, and the bond's destination is its *solution* (`created_puzzle_hash`), which
   nothing signs. The disputer's own coin spend only creates the launcher coin (by puzzle hash and amount)
   and is valid on its own. Taking that signed spend from the mempool and adding a launcher spend that pays
   itself, the thief gets the whole bond: `C5 ... ACCEPTED`, thief received 100,000,000,000 of
   100,000,000,000 (testnet bond; 5 XCH in production). A farmer can do it directly; a mempool observer by
   replacement, adding the singleton spend with an action of its own to cover the coin set.
2. **NEW-2 (Medium): the dispute's refund address can be rewritten.** `disputer_ph` is a free field of the
   dispute leaf's solution, bound by nothing the disputer signs: the same published spends with
   `disputer_ph = thief` are accepted, and an upheld dispute refunds the thief (`C6`).
3. **NEW-3 (High): the creator buys the holders' final accrual for a bond.** `reclaim.rue` waits only for
   `end + claim_window`; it does not look at a pending or disputed root. The creator disputes its own final
   root just before it activates; the root then waits for `resolve`, and the creator reclaims as soon as the
   window opens: `C7 ... ACCEPTED 970000`; after the guardian rejects the dispute and the root activates,
   Alice's claim is refused (the campaign is gone). Profitable whenever the final root's unclaimed accrual
   exceeds `dispute_bond`. Only our tools' `reclaim_blockers()` stand in the way, and a creator does not need
   our tools.
4. **FLPCR-08 (Medium, testnet High): the claim-window floor is too short.** See the table. The floor
   counts one `dispute_blocks` per root, but a root activates `HEIGHT_WINDOW + dispute_blocks` after its
   `h`, and the next root's `h` must reach the previous root's `activates_at` to promote it.

Suggested directions (applied on `fix-bond-reclaim`; the floor ended at `4 × (D + 32)`, because at
`3 × (D + 32)` a late veto leaves holders zero blocks to claim the final root):
- NEW-1/2: curry `bond_puzzle_hash`, `distributor_id` and `disputer_ph` into the bond launcher (so the
  disputer's signed spend commits to them through the launcher's puzzle hash), and have `dispute` derive
  that puzzle hash from `CONFIG` and its `disputer_ph`.
- NEW-3 and FLPCR-08: refuse `reclaim` while a dispute is open or while the pending root's totals name the
  campaign; and/or raise the floor to `3 × (dispute_blocks + HEIGHT_WINDOW)`.

## Other notes

- **Owner rule "the fee lookback excludes the current block":** it does not. `Weights.lookback_share`
  averages the `fee_window` blocks ending at the swap's height, inclusive, so a deposit in the swap's block
  counts 1/`fee_window`. A tiny `fee_window` (no floor above 1) does not reopen FLPCR-01: buckets shrink with
  it, and a one-block position still earns one block's share (`r01`).
- **`RouterRules()` defaults set no floor:** a 1-mojo campaign is routable until the owner sets
  `min_amount` / `min_per_day` (`r04`); the per-creator cap (4) is Sybil-able. The admin reclaim is what
  bounds the table lock: at most `max_campaign_length` ahead + length + window (about three years at the
  production maximums) per griefer row.
- **Testnet bundle export** and the mempool races (FLPCR-12, NEW-1 by replacement) remain uncovered by the
  simulator.

## Cost (`fix-bond-reclaim` leaves, XCH and CAT)

Singleton CLVM cost is the singleton spend's execution cost (action layer, merkle proof, leaf). Whole bundle
is `chia_wallet_sdk.spend_bundle_cost` of the bundle the simulator accepted: singleton, reserve, slots, bond
launcher and bond, and the stand-in coins that send messages, pay fees and fund deposits (per-byte,
`CREATE_COIN` and `AGG_SIG` charges included). Block limit 11e9. `cost.py`, recorded in `output/cost.txt`.

| Action | Singleton CLVM cost | Whole bundle cost | % of block |
|---|---:|---:|---:|
| XCH create (empty table) | 3.25M | 94.3M | 0.86% |
| XCH create (32nd, 31 in the table) | 4.13M | 139.5M | 1.27% |
| XCH post_root, 1 campaign | 3.03M | 85.7M | 0.78% |
| XCH post_root, 32 campaigns (max_campaigns) | 6.02M | 147.5M | 1.34% |
| XCH claim, first (insert, coin message), 32 in state | 6.06M | 192.8M | 1.75% |
| XCH post_root, 32 campaigns, with previous totals | 8.68M | 165.7M | 1.51% |
| XCH claim, repeat (own slot), 32 in state | 5.96M | 196.7M | 1.79% |
| XCH claim, first, key signature (AGG_SIG_UNSAFE) | 6.07M | 191.1M | 1.74% |
| XCH delegate (grant, insert) | 4.14M | 167.7M | 1.52% |
| XCH claim, delegated (delegate signs) | 6.14M | 201.4M | 1.83% |
| XCH reclaim (creator, coin message) | 4.88M | 157.2M | 1.43% |
| XCH reclaim (admin) | 4.84M | 155.7M | 1.42% |
| XCH dispute (bond launcher + bond) | 1.87M | 88.4M | 0.80% |
| XCH resolve (upheld, bond refund) | 1.80M | 83.5M | 0.76% |
| CAT create (empty table) | 3.25M | 143.8M | 1.31% |
| CAT create (32nd, 31 in the table) | 4.13M | 189.1M | 1.72% |
| CAT post_root, 1 campaign | 3.03M | 110.7M | 1.01% |
| CAT post_root, 32 campaigns (max_campaigns) | 6.02M | 172.6M | 1.57% |
| CAT claim, first (insert, coin message), 32 in state | 6.09M | 218.0M | 1.98% |
| CAT post_root, 32 campaigns, with previous totals | 8.68M | 190.8M | 1.73% |
| CAT claim, repeat (own slot), 32 in state | 5.99M | 221.9M | 2.02% |
| CAT claim, first, key signature (AGG_SIG_UNSAFE) | 6.10M | 216.3M | 1.97% |
| CAT delegate (grant, insert) | 4.14M | 192.8M | 1.75% |
| CAT claim, delegated (delegate signs) | 6.17M | 226.6M | 2.06% |
| CAT reclaim (creator, coin message) | 4.88M | 182.3M | 1.66% |
| CAT reclaim (admin) | 4.84M | 180.8M | 1.64% |
| CAT dispute (bond launcher + bond) | 1.87M | 113.5M | 1.03% |
| CAT resolve (upheld, bond refund) | 1.80M | 108.6M | 0.99% |

Against the review's eight-leaf XCH figures (create 2.45M / 79.7M, post_root 1 campaign 2.76M / 81.2M,
claim 4.43M / 123.2M, post_root 32 campaigns with totals 8.32M / 148.4M, claim with 32 campaigns 5.46M /
172.5M): every action is still under about 2% of a block; a CAT reward adds about 25M to the bundle (the
CAT layer on the reserve and deposit).
