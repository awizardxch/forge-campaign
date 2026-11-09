# FLPCR: checks that need Sage

> Written 2026-11-09 for the local agent that drives Sage. Everything else is proven on the simulator
> (`python -m pytest tests -q`). These steps are not, because each one depends on what a real wallet
> signs, or on a real chain.
>
> **Rules:**
> - testnet11 only;
> - one script at a time per `FLPCR_STATE`;
> - commit the record of each step to `docs/TESTNET.md` §5, never the state files.
>
> Tick a box only with evidence: a coin id, a `push_tx` result, or a balance change.
>
> **Start with a new distributor.** The 2026-11-09 re-pins (NEW-1 to NEW-4, FLPCR-08) moved every
> distributor's puzzle hash. Run `launch` on current `main` (`python contracts/build.py --check` clean,
> compiled fingerprint `6a987d3c…`) with a fresh `FLPCR_STATE`, and use it for every step below. The
> claim-window floor on testnet is now 160 blocks.

## A. Signatures (TESTNET.md §3 step 4)

- [ ] **A1. Address links (v2).** Have Sage sign both sides of a link with `signMessageByAddress`:
  - primary: `flpcr-link-v2:<distributor>:<primary>:<member>:<seq>`;
  - unlink: `flpcr-unlink-v2:…`.

  Check that `flpcr.links.verify` accepts both. Then check that a link replayed with an old `seq` is
  dropped by `LinkBook`. *Evidence:* the two signatures and verify's output.
- [ ] **A2. Claim leaves over WalletConnect.** The page flow in `docs/FORGE_SITE_TAB.md` §4.3:
  - sign the claim stand-in through `chip0002_signCoinSpends`, not Sage's local RPC;
  - check that the aggregate verifies as `AGG_SIG_UNSAFE(pk, leaf)` for every pair in
    `fixtures/vectors.json` → `claim_stand_in`.

  `claim-all` already proved the local-RPC path on distributor v5.
- [ ] **A3. Reclaim and delegate `AGG_SIG_ME`.** Run `reclaim` and `delegate --grant 1` / `--grant 0`
  on a current-pin distributor. Check that the grant and revoke each leave `delegate_fee` (1,000,000
  mojos on testnet) as fee.
- [ ] **A4. Create.** Run `create`. The creator's coin spend must send `"k" + id` (mode 23) next to the
  router's message. Check that a stale singleton coin makes Sage's signed spend fail, and that a re-sign
  against the current coin succeeds. That re-sign is the case the create intake's lease exists for.

- [ ] **A5. Dispute funding spend (NEW-4).** Run `dispute` on the new distributor, then `resolve
  --upheld 1`, repost, `dispute`, `resolve --upheld 0`. Check:
  - Sage signs the disputer's spend with `ASSERT_CONCURRENT_SPEND` (64) of the singleton coin in it;
  - the launcher sits at `bond_launcher_ph(disputer_ph, pending root)` and the bond coin appears at
    nonce 3 with exactly 0.1 TXCH;
  - the upheld bond returns to the disputer and the rejected one reaches the dev multisig;
  - pushing the funding spend alone (without the singleton spend) is refused with
    `ASSERT_CONCURRENT_SPEND_FAILED` (the simulator names it `AssertConcurrentSpendFailed`).

## B. Paths that have never run live

- [ ] **B1. CAT rewards.**
  1. `launch --reward-asset <a CAT in a V16 pool>`;
  2. `create` with a CAT deposit;
  3. `post`, `claim-all`, then `reclaim` after the window.

  Check that the reserve CAT ring stays whole (`status` against the chain) and that `resync` rebuilds
  the CAT distributor byte-equal.
- [ ] **B2. Lock claim.** From a 🔐 Forge lock, execute a proposal whose spend sends mode-16
  `"k" + leaf` for a campaign the lock earned in. Submit the bundle to the service's `POST
  /api/v1/claims`. The reward must pay the lock's address.
- [ ] **B3. Fee-weighted LP campaign.** Run a
  `fees_bps > 0` campaign on a V16 pool with swaps. Post twice from a fresh engine state, and check that
  both roots are identical. Check that a one-block LP deposit plus a dust swap earns about nothing
  (FLPCR-01, live).

## C. The hosted service

- [ ] **C1. Service against a testnet distributor.** Run `python -m flpcr.service.main` with a fresh
  operator key. Check:
  - it boots by resync;
  - `GET /api/v1/status` matches `deploy_testnet.py status`;
  - `GET /api/v1/claimable/<addr>` returns leaves that `claim-all` accepts.
- [ ] **C2. Rotate the operator to the service.** The guardian proposes with `rotate --to <service
  operator ph>` (the guardian is derivation 3 until it is a multisig). Wait `rotate_delay` (24 blocks on
  testnet), then check that the service posts and the old key is refused.
- [ ] **C3. Sequencer under load.** Submit claim requests from several wallets in one batch interval.
  Check that one bundle pays them all and that a bad request is refused on its own without sinking the
  batch.
- [ ] **C4. Watcher.** Post a deliberately wrong root from a
  test operator key. The watcher must report MISMATCH before `activates_at`, and its prepared dispute
  (signed by the `disputer` role, with the `assert_concurrent_spend` ids from its JSON) must stop the
  root activating.
- [ ] **C5. Create intake.** `POST /api/v1/campaigns/quote`, sign the creator's spend in Sage within the
  lease, `POST /api/v1/campaigns/create`. Check that a lapsed lease is refused and a re-quote succeeds.
- [ ] **C6. Follow.** While the service runs, spend the singleton from `deploy_testnet.py` (a claim).
  The service must follow the foreign spend without a restart and keep `status` equal to the chain.

## D. Before mainnet (owner)

- [ ] **D1. Guardian as a Forge multisig safe.** Run `resolve` and a rotation from the safe, not from
  derivation 3.
- [ ] **D2. Dev multisig as `dev_fee_ph` and `admin_ph`.** On mainnet these come from
  `VITE_CFMM_POOL_DEV_FEE_ADDRESS_MAINNET`, never hard-coded. Run one admin reclaim from it on testnet
  first.
