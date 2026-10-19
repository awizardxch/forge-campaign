# FLPCR pre-testnet review: findings

> **Date:** 2026-10-19
> **Revision:** `dea1d7d` (the `main` branch).
> **Compiled fingerprint:** `(cd contracts/compiled && sha256sum *.hex | sha256sum)` = `d3715b6b47ada4b80b7e3bf51a206a5e7341418119ce76f24486f9c8df2a4693`.
> **Method:**
> - The forge-puzzles runbook (`awizardxch/forge-puzzles`, `skills/n-asset-pool-audit/SKILL.md`) and
>   aWizard Familiar `docs/skills/clvmPuzzleAudit.md`, for the security lanes.
> - Cursor's thermo-nuclear code quality review (pointer in Familiar `docs/skills/thermoNuclearCodeReview.md`),
>   for the maintainability lane.
>
> **Status: findings only.** No fix has been applied, so the runbook's completion gate is **not met**. Each
> remediation below is a proposal that needs sign-off. Several touch decisions already made (§ Decisions
> needed).

## Summary

The review found **no path that moves reserve funds anywhere other than to the holder or creator who is
owed them**. The reserve invariant `state.reserve == Σ(budget − claimed) == reserve coin amount` held
after every accepted and refused step in every probe.

What it did find, ranked:

| ID | Risk | Title | Who can do it |
|---|---|---|---|
| FLPCR-01 | High | A one-block LP deposit plus a dust swap takes the whole fee-weighted half of a campaign | anyone |
| FLPCR-02 | High | Any key passes as any holder's or creator's delegate, with no grant | anyone |
| FLPCR-03 | High | The dispute window is up to 31 blocks shorter than configured; zero on testnet | operator key |
| FLPCR-04 | High | The campaign table only drains through creator reclaims; `create` can be locked for good | anyone (32 mojos) |
| FLPCR-05 | High | The two height conditions behind every timing rule are pinned by no test or mutant | process |
| FLPCR-06 | Medium | The guardian alone can rotate itself in as operator and post a root in one spend | guardian key |
| FLPCR-07 | Medium | `create` binds neither creator nor funder; a malformed creator locks the budget | responder / router |
| FLPCR-08 | Medium | The minimum claim window leaves no time for the final root; one veto hands it to the creator | creator + veto |
| FLPCR-09 | Medium | Where the operator cuts roots moves fee-weighted value between holders, and watchers can't see it | operator |
| FLPCR-10 | Medium | The NFT collection reader counts forged NFTs, and may miss real ones | anyone (with a DID side effect) |
| FLPCR-11 | Medium | Address links can be replayed, backdated, and cannot be revoked by the primary | anyone holding a published link |
| FLPCR-12 | Medium (provisional) | Anyone can spend the singleton for free and invalidate every prebuilt bundle | anyone |
| FLPCR-13 | Medium | Batched spends the docs promise are refused with `DuplicateOutput` | self-inflicted |
| FLPCR-14 | Low | Granting twice and then revoking once leaves the delegate active | holder's own mistake |
| FLPCR-15 | Low | A campaign may start up to 31 blocks in the past, and holders lose that accrual | creator |
| FLPCR-16 | Low | Two probes pass for the wrong reason; refusals don't pin which rule fired | process |
| FLPCR-17 | Low (provisional) | Float fee weights silently break exact arithmetic | adapter bug |
| FLPCR-18 | Info | Small findings: details in § Informational | — |

**Code quality** (§ Code quality): the review does not approve the code as it stands. The main item is that the driver/simulator boundary has
to move before the testnet deploy script copies it. None of the fixes moves a puzzle hash, except one
optional change.

**Gate for testnet:** fix FLPCR-02, -03, -05 and -07 (the creator-length check) in the puzzles, and
FLPCR-01 in the engine, before any campaign with real users. FLPCR-04 and -06 need a decision first. The
rest can land during testnet.

## Running the probes

Every finding's PoC is in `reviews/2026-10-19/probes/<lane>/`. Each attack runs next to an honest control
in the same world. From the repo root, with the toolchain from `CLAUDE.md`:

```bash
python contracts/build.py --check          # must match the fingerprint above (or re-run against the new one)
d=reviews/2026-10-19/probes
PYTHONPATH=.:$d/A python $d/A/a4_verify.py
```

The lanes are:
- **A:** authorization and message binding.
- **B:** slots and the sorted list.
- **C:** value conservation and arithmetic.
- **D:** state machine, time and liveness.
- **E:** off-chain engine, NFT reader and links.
- **F:** spec/puzzle/model/driver consistency, and the mutation helper `mut1.py`.

**Testnet11 verification:**
- No finding here has been run on testnet11. The commands below are the procedure, not a record.
- They need `scripts/deploy_testnet.py` (CLAUDE.md, step 2) and a way to export a probe's bundle as JSON
  instead of pushing it to the simulator. Neither exists yet.
- Run them only against a distributor we launch with throwaway keys, from an isolated testnet wallet.
- The shape is always the same:

```bash
# isolated testnet11 distributor, test keys only
python <probe> --network testnet11 --dump bundle.json   # bundle export: to be built
chia rpc full_node push_tx -j bundle.json               # full node RPC, not the wallet
chia rpc full_node get_coin_records_by_puzzle_hash '{"puzzle_hash": "0x…"}'   # evidence
```

---

## Findings

### FLPCR-01 - A one-block LP deposit plus a dust swap takes the whole fee-weighted half of a campaign

- **Risk Level:** High
- **Asset Class Impacted:** Native XCH and Standard CAT rewards (off-chain allocation; no puzzle involved)
- **Target Source Block:** `flpcr/engine.py` `accrue()` (`fee_points`, lines 193–240); SPEC §6.3, §6.7

#### 1. Observed Condition & Theoretical Exploit

`accrue` splits the window's fee budget (`fees_bps` of the window's emission) across fee events by
`weight × eligible LP share at that block`.

- **The exploit:**
  - With a single swap in a window, its weight cancels out: a 1-mojo-fee swap carries the entire fee
    budget.
  - The attacker adds large LP at block `h`, swaps dust at `h`, and leaves at `h+1`.
  - They take about their LP share of the window's whole fee budget, for one block of holding.
- **When other swaps happen:** the attacker only needs to match their total weight.
- **Cost:** the fee paid comes back mostly to the attacker, who is the majority LP for that block.
- **Spec contradicted:** this breaks SPEC §6.7 ("self-trading pays only when fee weight per mojo is worth
  more than the fee"; "a flash deposit is worth one block's share").
- **Who and how much:** no key is needed. Honest LPs lose up to `fees_bps` of every campaign.

#### 2. Local Simulator PoC Blueprint

`probes/E/e_engine.py`. This is the real `accrue` and `run_epoch`, and it was reproduced by the lead. The
setup is a 50/50 time/fee split, budget 100,000, and campaign `[0, 1000)`. The honest holder has 1,000 LP
throughout; the attacker has 99,000 LP at block 999 only, plus one weight-1 swap.

```
control (quiet pool):                {'aaaa': 100000}
attack, one root [0,1000):           {'aaaa': 50450.5, 'eeee': 49549.5}
attack vs honest swap of equal fee:  {'aaaa': 75200.5, 'eeee': 24799.5}
```

The attacker holds for 0.1% of the time and takes 49.5% of the rewards.

#### 3. Testnet11 Verification Command

This is engine-only, so no bundle is needed. Run a `fees_bps = 5000` campaign on a testnet V14 pool. From
a test wallet, add large LP, swap 1 mojo, and remove the LP in the next block. Then:

```bash
python -m flpcr root --source live --distributor <id> --end <h+2>    # once the live Source exists
```

**Evidence:** the test wallet's `earned` against an honest LP's.

#### 4. Remediation Code

This is engine-only: no puzzle hash moves, but the change is consensus-relevant for every watcher, so
SPEC §6.3 must change with it. Value each fee event against the holder's **time-averaged** share over a
lookback, not the instantaneous share:

```python
# engine.py, inside accrue(): replace bal_at(event.height) with an average over [height - L, height)
def lookback_share(holdings, h, L):
    lo = max(campaign.start, h - L)
    acc = holdings.integrate(lo, h)          # Σ balance × blocks, exact Fraction
    return {ph: Fraction(v, h - lo) for ph, v in acc.items() if v}
```

`L` would be about 1 day. Fix it per campaign in `Params`, so it is committed in the campaign id. A
one-block deposit then counts as 1/L. Two alternatives:
- add a minimum holding age before LP counts toward fee events;
- or cap each event's share of the budget at `fee_paid × rate`, with the remainder falling back to time
  weighting.

**Regression:** add `e_engine.py`'s attack to `tests/test_engine.py`, with the attacker's share ≤ 1% as a
positive bound and the control unchanged.

### FLPCR-02 - Any key passes as any holder's or creator's delegate, with no grant

- **Risk Level:** High
- **Asset Class Impacted:** Native XCH and Standard CAT
- **Target Source Block:** `contracts/puzzles/flpcr/shared.rue` `authorize_or_delegate` (lines 245–256), `spend_delegation` / `create_delegation` (266–271)

#### 1. Observed Condition & Theoretical Exploit

In mode 2 (`AUTH_DELEGATE`), the puzzle "proves" a grant exists by emitting `spend_delegation(owner,
delegate)` and `create_delegation(owner, delegate)` together, at the **same** slot puzzle hash.

1. The slot-spend message is puzzle-to-puzzle (mode 18), so any coin at that puzzle hash can receive it.
2. The recreated slot is a child of the current singleton coin, so it passes the slot puzzle's parent
   check.
3. That child can be spent **ephemerally in the same bundle**. The spend's own output then satisfies its
   own proof.

The result: any key can be the delegate of any holder for `claim`, and of any creator for `reclaim`.

- **Funds:** payouts still go only to the holder or the creator, so nothing is stolen.
- **Decisions broken:** "only the LP holder can claim" and "reclaim is creator-only".
- **Concrete harm:** anyone can force the creator's reclaim at `end + claim_window`. That ends every
  holder's late claim, which the creator had chosen to leave open. Anyone can also trigger claims for
  vaults and cold wallets on their own schedule.
- **Docs:** the trap written in the Familiar skill ("a slot created in a spend cannot be spent in the same
  block") is false, and this exploit is what it misses.

#### 2. Local Simulator PoC Blueprint

`probes/A/a4_verify.py` (claim) and `probes/A/a4_reclaim.py` (reclaim). Reviewer A wrote the probe; it was
interrupted, and the lead completed and ran it. The attack and the control differ by exactly one coin
spend: the ephemeral slot.

```python
spends = dedupe(w.d.spend([claim_action(..., auth=AUTH_DELEGATE,
                                        auth_data=(thief.puzzle_hash, (AUTH_SIGNATURE, thief.pk)))])[0])
spends.append(CoinSpend(Coin(w.d.coin.coin_id(), delegation_ph(holder, thief), 0),   # the slot this spend creates
                        delegation_puzzle, lineage(current singleton)))
```

```
XCH ungranted delegated claim, ephemeral slot spend=False: REFUSED (MessageNotSentOrReceived)
XCH ungranted delegated claim, ephemeral slot spend=True:  ACCEPTED   paid to holder: 100
CAT ... False: REFUSED   CAT ... True: ACCEPTED
XCH stranger reclaim, ephemeral=False: REFUSED   ephemeral=True: ACCEPTED  paid to creator: 970000
CAT stranger reclaim, ephemeral=False: REFUSED   ephemeral=True: ACCEPTED  paid to creator: 970000
```

The control is refused for the intended reason: without a grant, no slot coin can receive the message.

#### 3. Testnet11 Verification Command

On a test distributor past a campaign's `end + claim_window`, a fresh key builds the reclaim bundle with
the ephemeral slot spend and pushes it.

- **Expected mempool result:** accepted, with the creator's balance going up by `budget − claimed`.
- **Evidence to collect:** the push result, and `get_coin_records_by_puzzle_hash` on the delegation-slot
  hash, which should show no unspent coin before or after.

#### 4. Remediation Code (Rue)

The spent slot and the recreated slot must never share a puzzle hash. The claim slots already follow this
rule, because `claimed` strictly increases.

**The fix:** give the delegation slot a use counter. A use spends value `n` and recreates value `n + 1`:

```rue
// shared.rue
export inline fn delegation_value_hash(holder: Bytes32, delegate: Bytes32, uses: Int) -> Bytes32 {
    tree_hash((holder, (delegate, uses)))
}
struct Delegated { delegate: Bytes32, uses: Int, auth: Int, data: Any }
// authorize_or_delegate, AUTH_DELEGATE branch:
[
    spend_slot(config.delegation_slot_1st_curry_hash, delegation_value_hash(owner, d.delegate, d.uses)),
    create_slot_with_hint(config.delegation_slot_1st_curry_hash,
                          delegation_value_hash(owner, d.delegate, d.uses + 1), owner),
    authorize(d.delegate, d.auth, d.data, message),
]
```

- `delegate.rue`'s grant creates `uses = 0`, and its revoke spends the current `uses`.
- The driver tracks `uses` per delegation.

**Better:** fold this into the unique-key list that FLPCR-14 needs, using the claim-slot pattern with
`(key, active, uses, left, right)`. One structure then fixes both findings.

- **Hash impact:** this moves every leaf hash (it changes `shared.rue`), so re-pin.
- **Cost:** about one more `+` and a slightly larger tree hash per delegated use.
- **Regressions:**
  - the ungranted claim with an ephemeral slot is refused (both assets);
  - the ungranted reclaim with an ephemeral slot is refused;
  - an honest delegated claim, used twice in two spends, is accepted;
  - a mutant probe on the `+ 1`.

### FLPCR-03 - The dispute window is up to 31 blocks shorter than configured; zero on testnet

- **Risk Level:** High
- **Asset Class Impacted:** Native XCH and Standard CAT (every emitted-but-unclaimed balance)
- **Target Source Block:** `post_root.rue:110` (`activates_at: h + CONFIG.dispute_blocks`); `shared.rue:92–93` (prologue)

#### 1. Observed Condition & Theoretical Exploit

The prologue bounds the solution's `h` to `[P − 31, P]`, where `P` is the inclusion height (`ASSERT_HEIGHT_ABSOLUTE h`,
`ASSERT_BEFORE_HEIGHT_ABSOLUTE h + 32`). So `h` can only be understated. `post_root` counts the window from
that `h`, which makes the real window `dispute_blocks − 31`.

- **Production (`dispute_blocks = 38`):** 7 blocks.
- **Testnet (8):** no window at all. The root promotes on the next spend.
- **Exploit:** a compromised operator key posts a tree paying its own addresses, with `end ≤ P − 31` and
  `h = P − 31`, and claims before the watcher can react.
- **What still bounds it:** the emission ceiling, but nothing else.
- **What it contradicts:**
  - SPEC §5.2/§7: the operator "cannot make a root claimable before its dispute window ends";
  - AGENT_TOOLS `flpcr.watch`: "veto inside `dispute_blocks`";
  - the claim-window floor's own rationale.
- **Missing config check:** nothing requires `dispute_blocks > HEIGHT_WINDOW`.

#### 2. Local Simulator PoC Blueprint

`probes/D/d1_window.py`, reproduced by the lead; also `probes/F/probes.py` 3a/3b.

```
D=38 honest h=P:     first successful claim at P+38
D=38 attack h=P-31:  first successful claim at P+7   (363750 paid)
D=8  honest h=P:     P+8
D=8  attack h=P-31:  P+1
```

#### 3. Testnet11 Verification Command

On a test distributor (`dispute_blocks = 8`), post a root with `end ≤ tip − 31` and `h = tip − 31`, then
push a claim in the next block.

- **Expected result:** both are accepted.
- **Evidence to collect:** the `post_root` and claim inclusion heights.

#### 4. Remediation Code (Rue)

```rue
// post_root.rue:110
activates_at: h + HEIGHT_WINDOW + CONFIG.dispute_blocks,
```

Because `P ≤ h + 31`, a root is never claimable fewer than `dispute_blocks` blocks after it lands.

- Leave `promote` as it is: understating a later `h` only delays promotion.
- Update the mirror in `flpcr/distributor.py` (`post_root`) to match, and the SPEC/AGENT_TOOLS wording
  ("at least `dispute_blocks` after inclusion, up to 31 more").
- **Hash impact:** moves `post_root`'s pin.
- **Regressions:**
  - lagged-`h` post: no claim succeeds before `P + dispute_blocks` (both assets, both clocks);
  - honest post: unchanged;
  - mutant on the `HEIGHT_WINDOW` term.

### FLPCR-04 - The campaign table only drains through creator reclaims; `create` can be locked for good

- **Risk Level:** High (liveness; no funds lost)
- **Asset Class Impacted:** Native XCH and Standard CAT (distributor availability)
- **Target Source Block:** `create.rue:66` (`count(state.campaigns) < CONFIG.max_campaigns`), `create.rue:54–56`; `reclaim.rue:27` (creator-only); `flpcr/router.py` `check_campaign`

#### 1. Observed Condition & Theoretical Exploit

A campaign leaves `state.campaigns` only through its creator's reclaim, and nothing forces one.

- **Griefing:** the router signs anything `check_campaign` accepts, and there is no minimum amount (SPEC
  §11 Q1 is still open).
  - Each campaign with `amount = 1` pays a fee of 0 and has a budget of 1.
  - 32 of them, with `start` far in the future or 365-day length and window, fill the table. The griefer
    never reclaims.
  - **Cost:** 32 mojos plus network fees.
- **Honest wear:** a fully claimed campaign (`claimed == budget`) keeps its row, and its creator has no
  reason to reclaim 0. 32 of those over the distributor's life fill it permanently.
- **Recovery:** launch a new distributor.

#### 2. Local Simulator PoC Blueprint

`probes/D/d3_fill.py` (reproduced by the lead), `probes/C/probe_c.py` P1 and `probe_c2.py` Q1.

```
campaigns 32 reserve 32 griefer deposit total mojos 32
33rd honest campaign: REFUSED Eval error: clvm raise
stranger reclaim of a griefer campaign: REFUSED Eval error: clvm raise
max_campaigns=2, c1 fully claimed: next create refused, now and 1000 blocks after c1's window
```

The control is the same honest create in a fresh world: accepted.

#### 3. Testnet11 Verification Command

On a test distributor launched with `max_campaigns = 2`, create two 1-mojo campaigns. Then dry-run a
third create; do not push it.

- **Expected result:** the leaf raises.
- **Also check:** the API quote route signs a 1-mojo campaign.

#### 4. Remediation Code (Rue): needs a decision (§ Decisions needed)

**Two changes that keep "reclaim is creator-only":**

```rue
// create.rue: bound how far ahead a campaign may start
assert start <= h + CONFIG.max_campaign_length;
```

```python
# router.py check_campaign: a minimum budget and per-day rate (SPEC §11 Q1), and a per-creator rate limit
```

**The structural fix, which needs the decision:** let anyone remove a campaign, paying the leftover to
`campaign.creator` only. Allow it either when nothing is left (`claimed == budget`), or after a grace
period:

```rue
// reclaim.rue
let sweepable = (campaign.claimed == campaign.budget) | (h >= campaign.end + 2 * campaign.claim_window);
let authorization = if sweepable { nil } else {
    authorize_or_delegate(CONFIG, campaign.creator, auth, auth_data, message)
};
```

The first condition removes an empty row and pays nothing, so it changes nobody's position. The second
lets a stranger end late claims at `end + 2 × window`.

- **Hash impact:** moves the `create` and `reclaim` pins.
- **Regressions:**
  - the 33rd create succeeds after a sweep;
  - a stranger's sweep pays only the creator;
  - a sweep before eligibility is refused.

### FLPCR-05 - The two height conditions behind every timing rule are pinned by no test or mutant

- **Risk Level:** High (test blind spot under FLPCR-03, -08 and -15, and under every "not before" rule)
- **Asset Class Impacted:** Native XCH and Standard CAT
- **Target Source Block:** `shared.rue:92–93` (`AssertHeightAbsolute`, `AssertBeforeHeightAbsolute`); `scripts/mutate.py:76` (mutates only lines starting `assert `)

#### 1. Observed Condition & Theoretical Exploit

SPEC §12 says every leaf assertion is pinned by a probe. But the two *emitted* conditions that keep `h`
honest are never mutated, and no test fails without them.

Without `AssertHeightAbsolute`, `h` can be overstated. That opens:
- early reclaim;
- roots dated in the future;
- instant promotion.

The suite stays green throughout.

#### 2. Local Simulator PoC Blueprint

`probes/F/mut1.py`, reproduced by the lead:

```
python $d/F/mut1.py shared.rue "AssertHeightAbsolute {" tests/test_chain.py tests/test_differential.py
-> 92 passed            (likewise for "AssertBeforeHeightAbsolute {")
```

On that mutant, `probes/F/futureh.py` has the creator reclaim the full budget at height 6, for a campaign
starting at 8, by naming `h = 63`. The original build refuses it with `AssertHeightAbsoluteFailed`.

#### 3. Testnet11 Verification Command

Not applicable: this is a test-suite gap.

#### 4. Remediation Code

```python
# tests/test_chain.py, TestAssertionProbes
def test_h_cannot_be_in_the_future(self):   # expect AssertHeightAbsoluteFailed
def test_h_cannot_be_stale(self):           # h = tip - 32: expect AssertBeforeHeightAbsoluteFailed
def test_lagged_h_keeps_the_dispute_window(self)   # FLPCR-03
```

- `mutate.py` also deletes lines that emit conditions (`Assert*`, `ReceiveMessage`, `AggSigMe`).
- `refused()` gains an `expect=` argument for the consensus error code (FLPCR-16).
- Add a CAT twin of `TestAssertionProbes`.

### FLPCR-06 - The guardian alone can rotate itself in as operator and post a root in one spend

- **Risk Level:** Medium (needs the guardian key; the guardian is meant to be a multisig)
- **Asset Class Impacted:** Native XCH and Standard CAT
- **Target Source Block:** `rotate.rue:19` (`engine_ph: new_engine_ph`, effective immediately); `post_root.rue`

#### 1. Observed Condition & Theoretical Exploit

Actions apply in order within one spend. `[rotate(guardian_ph), post_root(…)]` therefore needs only the
guardian's two messages.

Since the guardian is also the only party who can veto, a compromised guardian can pay any split within
the emission ceiling to itself, with no check. This contradicts SPEC §7 ("Guardian: cannot post roots;
move funds") and the intended two-key separation.

#### 2. Local Simulator PoC Blueprint

`probes/A/p1_guardian_posts.py`, reproduced by the lead.

```
control: operator posts, alice claims 970000
negative control: guardian post_root without rotate: REFUSED (MessageNotSentOrReceived)
ATTACK: guardian rotate(self)+post_root in one spend: ACCEPTED
guardian gained 970000 of budget 970000; alice (the real holder) got 0      (XCH and CAT)
```

#### 3. Testnet11 Verification Command

Push the paired bundle from the test guardian against the tip.

- **Expected result:** accepted.
- **Evidence to collect:** `engine_ph` in the next singleton state.

#### 4. Remediation Code (Rue): needs a decision

**Delayed rotation.** `rotate` stores a pending key that takes effect only after a delay longer than the
dispute window. The watcher alerts on any pending rotation. This needs a new `State` field (a pin event
for every leaf):

```rue
// rotate.rue
State { ...state, pending_engine: (new_engine_ph, h + HEIGHT_WINDOW + CONFIG.rotate_delay) }
// prologue/promote: adopt pending_engine once h >= its height; post_root uses state.engine_ph only
```

This doesn't stop a guardian who waits out the delay. It does make the takeover public and slow, which
gives the multisig's other signers time to react.

**The alternative is documentation:** state in SPEC §7 that the guardian multisig is the root of trust for
roots as well as vetoes.

### FLPCR-07 - `create` binds neither creator nor funder; a malformed creator locks the budget

- **Risk Level:** Medium (trust model); Low (malformed creator)
- **Asset Class Impacted:** Native XCH and Standard CAT
- **Target Source Block:** `create.rue:17` `main(creator, …)`; AGENT_TOOLS `flpcr.create` ("give `amount`, request nothing")

#### 1. Observed Condition & Theoretical Exploit

- **No consent from the creator:** the only authorization on `creator` is the router's `"c" + id`. The
  deposit is enforced by conservation alone, so whoever builds the create spend chooses who can reclaim
  the leftovers.
- **The offer is a blank cheque:** a creator's Sage offer that "requests nothing" can be taken by whoever
  holds it. This contradicts "Router: cannot touch funds".
- **Wrong-length creators are accepted:**
  - 0, 31 and 33 bytes all pass `create`;
  - every reclaim path is then refused by consensus (`InvalidPuzzleHash`), and so is every delegate
    grant;
  - the unearned budget stays in the reserve forever.

#### 2. Local Simulator PoC Blueprint

`probes/A/p2_create_creator.py`, reproduced by the lead.

```
Alice's coin funds a campaign recorded with creator = mallory; mallory reclaims 970000
0/31/33-byte creator: create ACCEPTED; reclaim by message REFUSED (InvalidPuzzleHash),
  by signature REFUSED (clvm raise), grant REFUSED (InvalidPuzzleHash); reserve still holds 970000
control(b): 32-byte creator reclaims: ACCEPTED (970000)
```

#### 3. Testnet11 Verification Command

Push a create from a test router with a 31-byte creator, then try a reclaim.

- **Expected result:** the create is accepted and the reclaim is refused (`INVALID_PUZZLE_HASH`).

#### 4. Remediation Code (Rue)

```rue
// create.rue: the creator agrees to its own campaign id (mode 0 signature or mode 1 message)
fn main(h: Int, creator: Bytes32, ..., auth: Int, auth_data: Any) -> ... {
    assert strlen(creator) == 32;                       // check the length builtin's name in rue 0.8.4
    ...
    let consent = authorize(creator, auth, auth_data, id);
```

- **Funding:** a creator spend that both funds the reserve and sends `"k" + id` (mode 1). This replaces
  the bare offer, so the funder is the creator.
- **The same length check** also goes on `delegate` and `holder`/`puzzle_hash` in `delegate.rue` and
  `claim.rue`. Reviewer A did not probe 0/31/33-byte values for `puzzle_hash`, `target` or `nonce`.
- **Hash impact:** moves `create`, plus any leaf that gets the length check.

### FLPCR-08 - The minimum claim window leaves no time for the final root; one veto hands it to the creator

- **Risk Level:** Medium
- **Asset Class Impacted:** Native XCH and Standard CAT (holders' final accrual)
- **Target Source Block:** `create.rue:64` (`claim_window >= 2 * CONFIG.dispute_blocks`); SPEC §5.5

#### 1. Observed Condition & Theoretical Exploit

The `2 × dispute_blocks` floor assumes the root covering `end` can be posted at `end`. But the previous
root can still be pending then, which delays the final post by up to `dispute_blocks`.

- **Under honest cadence:** the final root activates in the same block that reclaim opens, so holders have
  0 blocks of claim time.
- **After one veto, or one lost singleton race (FLPCR-12):** reclaim opens before the final root
  activates.
- **The incentive:** a creator who wants the holders' accrual back picks the minimum window.

#### 2. Local Simulator PoC Blueprint

`probes/D/d7_final.py`, reproduced by the lead.

```
r2 covering end posted at 55 activates 63 ; reclaim opens at 63
claimable blocks before reclaim opens: 0
after one veto r2 re-posted, activates 66 reclaim opens 63
creator reclaims before r2 activates: ACCEPTED 970000 (holders got 0)
```

#### 3. Testnet11 Verification Command

Replay the timeline on a test distributor (`dispute_blocks = 8`, window 16). Veto once with the test
guardian.

- **Expected result:** the creator's reclaim is accepted while r2 is still pending.

#### 4. Remediation Code (Rue)

```rue
// create.rue:64: room for the previous root, one veto and repost, and the HEIGHT_WINDOW slack
assert claim_window >= 3 * CONFIG.dispute_blocks + HEIGHT_WINDOW;
```

- Mirror the new floor in `Config`/`distributor.py`.
- Show the effective claim time in the quote and the UI.
- **Hash impact:** moves `create`.

### FLPCR-09 - Where the operator cuts roots moves fee-weighted value between holders, and watchers can't see it

- **Risk Level:** Medium
- **Asset Class Impacted:** Native XCH and Standard CAT (off-chain allocation)
- **Target Source Block:** `flpcr/engine.py` `run_epoch` / `accrue` (the fee budget, and the time-weight fallback, are both decided per window)

#### 1. Observed Condition & Theoretical Exploit

The fee budget, and whether the time-weight fallback applies, are both decided per root window. The
operator picks each `end` and can skip windows where nothing accrued.

The same chain can therefore produce very different splits. Watchers replay with the posted boundaries,
so every cut verifies. A colluding operator can favour a holder, and the dispute process cannot see it.

#### 2. Local Simulator PoC Blueprint

`probes/E/e_engine.py`, reproduced by the lead, on the same chain as FLPCR-01:

```
one root [0,1000):          {'aaaa': 50450, 'eeee': 49549}
roots [0,998) + [998,1000): {'aaaa': 99851, 'eeee': 148}
```

#### 3. Testnet11 Verification Command

This is engine-only. Sweep the cut sets over recorded testnet fee events, and compare the payout spread.

#### 4. Remediation Code

Make the allocation independent of the cut. Allocate the fee budget per block (or per fixed, campaign-
relative bucket of N blocks), so that any set of root boundaries gives the same cumulative at each
boundary. Combine this with FLPCR-01's lookback.

- **Regression:** a property test where random cuts over the same events give identical cumulatives at
  shared heights.

### FLPCR-10 - The NFT collection reader counts forged NFTs, and may miss real ones

- **Risk Level:** Medium
- **Asset Class Impacted:** NFT Collection (gates and boosts)
- **Target Source Block:** `flpcr/chain/nfts.py` `collection()` (lines 68–90); SPEC §6.8 ("the walk cannot be fooled")

#### 1. Observed Condition & Theoretical Exploit

**Forged NFTs are counted (confirmed).**
- The walk puts any non-launcher child of a DID spend into the frontier, as a possible intermediate
  launcher.
- Any singleton launcher under such a child then counts as part of the collection.
- So if a DID spend ever creates a coin someone else controls (a memo or notification coin, a payment, an
  orphaned intermediate), that person can launch an NFT from it with no DID involvement, and it counts as
  a Wizard.

**Real NFTs may be missed (provisional).**
- The reference wallet's single "mint with DID" creates the launcher from the XCH funding coin. The DID
  only announces it.
- Those NFTs are not under a DID spend, so the reader drops them, and real holders fail the gate.
- Whether this affects the Wizards depends on how they were minted.

#### 2. Local Simulator PoC Blueprint

`probes/E/e_nft.py` (NftKit on the simulator), reproduced by the lead.

```
control collection == [real]: True
A: forged NFT counted as Wizard: True  collection size 2
B: DID-authorized single mint (launcher parent = XCH coin), owner DID set = True ; counted: False
```

#### 3. Testnet11 Verification Command

This is read-only, on mainnet data. Sample Wizard launchers, check each launcher's parent puzzle, and look
for DID spends with third-party outputs:

```bash
chia rpc full_node get_coin_record_by_name '{"name": "0x<launcher id>"}'
chia rpc full_node get_puzzle_and_solution '{"coin_id": "0x<parent>", "height": <h>}'
```

#### 4. Remediation Code

```python
# nfts.py collection(): at depth 2, accept a parent only if it is an intermediate launcher
if child.puzzle_hash != nft_intermediate_launcher_hash(did_launcher_id, mint_number, mint_total):
    continue
# and/or: accept an NFT whose eve spend's DID transfer asserted the DID's announcement in the same block
# (only the DID can announce); this also covers case B
```

- **Regressions:** case A refused, case B counted, the control unchanged.

### FLPCR-11 - Address links can be replayed, backdated, and cannot be revoked by the primary

- **Risk Level:** Medium
- **Asset Class Impacted:** Native XCH and Standard CAT (gate and boost allocation)
- **Target Source Block:** `flpcr/links.py` (`LINK_TAG` message, `LinkBook`); `flpcr/manifest.py` (links not committed); `flpcr/sources.py:98` `from_json` (drops `address_links`)

#### 1. Observed Condition & Theoretical Exploit

The signed message `flpcr-link-v1:<primary>:<member>` carries no distributor, sequence number or height.

- **Replay:** anyone holding a published link can re-record it later. That undoes the member's unlink
  without new consent.
- **No revoke for the primary:** an unlink needs the member's signature.
- **Backdating:** the recorded height is the operator's choice and isn't signed, so a link can be
  backdated for retroactive share.
- **Watchers can't check:** the link set is in neither the root manifest nor the chain, so watchers
  cannot reproduce a root that depends on links.
- **The CLI ignores links:** `FixtureSource.from_json` drops them.

#### 2. Local Simulator PoC Blueprint

`probes/E/e_links.py` (real BLS keys), reproduced by the lead.

```
control (link@10, unlink@50):  {'b42f': 1000}
replayed old link @150:        {'b42f': 750, '5df8': 250}
backdated link verify():       True
FixtureSource.from_json keeps links: False
```

#### 3. Testnet11 Verification Command

Against the API, resubmit a previously recorded link after an unlink.

- **Expected result (today):** accepted.

#### 4. Remediation Code

```python
# links.py
message = f"flpcr-link-v2:{distributor_id.hex()}:{primary}:{member}:{seq}"   # seq strictly increasing per pair
# LinkBook: reject seq <= last seen; accept an unlink signed by either side
# manifest.root_to_json: include sha256 of the cumulative link log; publish the log itself
# sources.FixtureSource.from_json: parse "links" with link_from_json
```

### FLPCR-12 - Anyone can spend the singleton for free and invalidate every prebuilt bundle (PROVISIONAL)

- **Risk Level:** Medium (provisional: the simulator cannot model the mempool race)
- **Asset Class Impacted:** Native XCH and Standard CAT (liveness of `post_root`, `veto`, `claim` and `create`)
- **Target Source Block:** `delegate.rue` (any address may grant itself a delegate); every message bound to the singleton's coin id

#### 1. Observed Condition & Theoretical Exploit

Any keypair can spend the singleton through `delegate`: no coin, no stake and no fee are needed. Every
operator, guardian, router and holder bundle names one singleton coin, and fails once someone else's spend
lands first.

- **The attack:** a bot toggles grant and revoke every block.
- **Why replace-by-fee doesn't help:** with mode 1 auth, the bot also spends a coin of its own, which
  defeats the mempool's superset rule.
- **What it breaks:** it can delay a veto (A-5) and the final root (FLPCR-08).

#### 2. Local Simulator PoC Blueprint

`probes/D/d4_race.py`:
- after a fresh wallet's delegate spend, the operator's prebuilt `post_root` is `REFUSED (DoubleSpend)`;
- rebuilt, it is `ACCEPTED`.

The race itself is untested.

#### 3. Testnet11 Verification Command

Run a 0-fee grant/revoke loop from a test key against a test distributor while the operator posts.
**Evidence to collect:** how many blocks pass before the operator lands.

#### 4. Remediation

- **In the puzzle:** require a fee in `delegate`: `ReserveFee { amount: CONFIG.delegate_fee }` (the cost
  scales with spam).
- **In the driver:** let the operator and guardian bundles carry pending delegate actions.
- **Operationally:** retry with a fee every block, and alert on contention.
- The puzzle option moves the `delegate` pin and adds a `Config` field.

### FLPCR-13 - Batched spends the docs promise are refused with `DuplicateOutput`

- **Risk Level:** Medium (an agent following the docs builds bundles that fail; no funds are at risk)
- **Asset Class Impacted:** Native XCH and Standard CAT
- **Target Source Block:** `shared.rue` `reserve_payment` (every payment is a `CREATE_COIN` from the one reserve coin); `authorize_or_delegate` (recreates the same delegation coin per action); `flpcr/chain/driver.py` `spend`. Docs: SPEC §5.4 and §9 ("Claim all"), API `POST /claims/build`, AGENT_TOOLS rule 4

#### 1. Observed Condition & Theoretical Exploit

A coin id is `(parent, puzzle hash, amount)`, so two equal outputs from the same coin in one spend
collide. The docs say claims "batch into one singleton spend as long as no two touch the same slot". In
practice more combinations fail:

| Combination in one spend | Result |
|---|---|
| One holder, two campaigns, equal payouts | `DuplicateOutput` |
| A delegate claiming two campaigns for one holder | `DuplicateOutput` (same delegation coin recreated twice); the driver also emits duplicate coin spends |
| A grant plus a delegated claim | `DuplicateOutput` |
| Two `create`s with the same amount (identical dev-fee outputs) | `DuplicateOutput`; the reference model accepts them |
| Two first claims on a fresh list (shared neighbour) | `DoubleSpend` |

#### 2. Local Simulator PoC Blueprint

- `probes/C/probe_c3.py` (reproduced by the lead): increments of 363,750 and 363,750 batched give
  `DuplicateOutput`; each one alone is accepted.
- `probes/C/probe_c.py` P3.
- `probes/F/probes2.py` and `probes3.py`.

#### 3. Testnet11 Verification Command

Build the batched bundle and push it to the test distributor.
**Expected result:** the mempool refuses it with `DUPLICATE_OUTPUT`.

#### 4. Remediation

- **No puzzle change.** `Distributor.spend` should detect collisions and raise, and a builder should
  split them across spends. The collisions are:
  - equal tagged `(ph, amount)` outputs;
  - a repeated delegation pair;
  - a shared neighbour slot.
- **Docs:** list the cases in AGENT_TOOLS rule 4, TESTNET §4 and API `/claims/build`.
- **Model:** the reference model needs a notion of the bundle before the differential suite can see any
  of this.

### FLPCR-14 - Granting twice and then revoking once leaves the delegate active

- **Risk Level:** Low (payouts still go to the holder or creator; the holder has to grant twice first)
- **Asset Class Impacted:** Native XCH and Standard CAT
- **Target Source Block:** `delegate.rue`; `shared.rue` `delegation_value_hash`; the driver's `delegations` dict; `distributor.py` `set_delegate`

#### 1. Observed Condition & Theoretical Exploit

Every grant of the same delegate creates a coin at the same puzzle hash, and a puzzle cannot prove that no
such coin exists already. So two grants leave two slots, and one revoke leaves one live.

The model and the driver both report the delegate as revoked. A holder who re-granted (for example a wallet
retry) and then revoked a compromised hot wallet hasn't cut it off. It still chooses when claims happen,
and when reclaims happen for a holder who is also a creator.

#### 2. Local Simulator PoC Blueprint

`probes/B/probe_slots.py` `delegation_probes()` (reproduced by the lead) and `probes/A/p3_double_grant.py`.
- Control (grant, revoke): the delegated claim is `REFUSED (DoubleSpend)`.
- Attack (grant, grant, revoke): the delegated claim is `ACCEPTED`, paying 485,000 to the holder.

#### 3. Testnet11 Verification Command

Grant twice and revoke once from a test wallet. Then:

```bash
chia rpc full_node get_coin_records_by_hint '{"hint": "0x<holder ph>", "include_spent_coins": false}'
```

**Expected result:** one unspent 0-mojo delegation slot is left.

#### 4. Remediation

- **On chain:** delegation records become a sorted unique-key list keyed by
  `tree_hash((holder . delegate))`, like the claim slots. The value is `(key, active, uses, left, right)`:
  - grant inserts the key, or sets `active` to 1;
  - revoke sets `active` to 0;
  - a delegated use asserts `active == 1` and increments `uses` (this is FLPCR-02's fix).
- **Interim, off chain:** refuse a grant when a slot hinted with the holder exists, and make revoke loop
  until none remains.

### FLPCR-15 - A campaign may start up to 31 blocks in the past, and holders lose that accrual

- **Risk Level:** Low
- **Asset Class Impacted:** Native XCH and Standard CAT
- **Target Source Block:** `create.rue:56` (`assert start >= h`); `engine.run_epoch` (accrues from `state.height`)

#### 1. Observed Condition & Theoretical Exploit

With `h = P − 31`, `start = P − 31` is accepted. If the last root's `end` is already past `start`, the
engine never accrues `[start, last.end)`. That part of the budget goes back to the creator at reclaim,
even though the campaign advertised and paid the fee on the full budget. This contradicts SPEC's "no
retroactive campaigns".

#### 2. Local Simulator PoC Blueprint

`probes/D/d2_retro.py`:
- `start = H − 31` is accepted;
- controls: `start = H − 32` gives `AssertBeforeHeightAbsoluteFailed` or `clvm raise`;
- 277,143 of 970,000 never reaches holders.

#### 3. Testnet11 Verification Command

Create with `start = h = tip − 31` from a test router.
**Expected result:** accepted.

#### 4. Remediation Code (Rue)

```rue
// create.rue:56
assert start >= h + HEIGHT_WINDOW;
```

- The router quotes `start ≥ tip + 32`.
- **Hash impact:** moves `create`.

### FLPCR-16 - Two probes pass for the wrong reason; refusals don't pin which rule fired

- **Risk Level:** Low (test hygiene)
- **Asset Class Impacted:** —
- **Target Source Block:** `tests/test_chain.py:333` (height-pin probe); `test_first_claim_cannot_insert_a_second_slot` (line 257); `refused()` (line 25)

#### 1. Observed Condition

**The height-pin probe kills its mutant with a malformed solution.**
- `Action("reclaim", [h', c.id])` has no `auth`. With the pin deleted, it fails with
  `path into atom`.
- `probes/F/pinprobe.py` adds the creator's authorization:
  - the original build refuses it (`clvm raise`);
  - the mutant accepts it and pays the creator 485,000 early.

**The first-claim probe never reaches the rule it is named after.**
- It is refused by the posted-total check.
- With that check deleted, it is refused by `CoinAmountNegative` instead (`probes/F/firstclaim.py`).

**`refused()` is too loose.**
- It accepts any `Simulator error`, including the `DuplicateOutput` and `DoubleSpend` that driver bugs
  cause.
- Every puzzle refusal is the same `clvm raise`, so AGENT_TOOLS rule 5 ("a `clvm raise` names a puzzle
  rule") is false.

#### 2. Local Simulator PoC Blueprint

`probes/F/pinprobe.py`, `probes/F/firstclaim.py` and `probes/F/codes.py`.

#### 3. Testnet11 Verification Command

Not applicable.

#### 4. Remediation

- Use `pinprobe.py`'s form for the height-pin probe.
- Add a whale holder to the first-claim test, as `test_insert_must_be_ordered` does.
- Add `refused(..., expect="AssertHeightAbsoluteFailed")` and similar, so each probe pins its consensus
  error code.
- For puzzle refusals, pin the rule by mutant, because `clvm raise` carries no rule.
- Correct AGENT_TOOLS rule 5.

### FLPCR-17 - Float fee weights silently break exact arithmetic (PROVISIONAL)

- **Risk Level:** Low (provisional: the live adapter does not exist yet)
- **Asset Class Impacted:** off-chain allocation
- **Target Source Block:** `flpcr/engine.py` `accrue`; `FixtureSource.from_json`

#### 1. Observed Condition

A fee weight valued in XCH at spot price tends to be a float. A float weight turns every credit into a
float, which breaks exact replay between implementations, and `state_to_json` then crashes.

#### 2. Local Simulator PoC Blueprint

`probes/E/e_float.py`, with weights 0.1 and 0.2:
- the credits come out typed `float`;
- then `state_to_json: AttributeError 'float' object has no attribute 'numerator'`.

#### 3. Testnet11 Verification Command

Not applicable until the live `Source` exists.

#### 4. Remediation

- Coerce weights to `int` or `Fraction` at the `Source` boundary, and reject floats.
- Define the valuation as an exact rational in SPEC §6.3.

---

## Informational

- **Late veto (D-6):** the guardian can veto up to 31 blocks after `activates_at`, as long as nobody has
  spent the singleton since (`probes/D/d1_window.py`). Document it as "active is lazy".
- **CAT deposit excess (C-3):** a CAT create cannot carry excess deposit. SPEC's "any excess is ordinary
  bundle slack" is XCH-only (`probe_c2.py` Q4: `AssertCoinAnnouncementFailed`). The driver should always
  add a CAT change output.
- **Blacklist and forwarding (E-7):** a blacklisted wrapper's LP is still forwarded to the wrapper's
  holders while forwarding is on. Document it, or check the blacklist against the wrapper too.
- **Decoy slots (B-2, reasoning only):** anyone can create a 0-mojo coin at a live slot's puzzle hash with
  the same hint. It can't be spent as a slot (`AssertMyParentIdFailed`), but a naive chain-sync reader
  could pick it. The reader must accept a slot only if its parent is a known distributor generation.
- **`insert_coin` caveat:** using `insert_coin` (simulator only), a fake singleton-looking parent is
  accepted. Tests must never fabricate slots that way.
- **NFT melt (gap):** `nft_events` never emits a final −1 for a melt or an unparseable spend, so the last
  owner may be counted forever. Not verified.
- **Doc drift (F-8):**
  - **SPEC:**
    - §5.1 `Config` lacks `delegation_slot_1st_curry_hash`;
    - §5 cites `contracts/v14/pins.json`;
    - §12 still says "fee collection";
    - §3.1 says the leaf checks `blacklist ∩ whitelist`, which it doesn't;
    - the §4 diagram says "anyone ── claim";
    - §5.4 says "one of two forms", then describes three;
    - §5.2 says "appends" (it prepends);
    - §5.3's ceiling formula lacks the `max(·, start)` clamp.
  - **Puzzles:** `shared.rue` and `delegate.rue` comments say delegation is claims-only.
  - **PUZZLES.md:** says upstream is "pinned by sha256", but `build.py` rewrites that hash and never
    compares it.
  - **TESTNET.md:**
    - §2 omits the delegation records;
    - its test time (~4 s) disagrees with SPEC's (about 7 s).
  - **AGENT_TOOLS:**
    - rule 4 says "adjacent" (any shared neighbour collides);
    - `flpcr.quote` omits the `start` and window-maximum checks.
  - **Familiar skill:** the "slot created in a spend cannot be spent in the same block" trap is wrong
    (FLPCR-02).

## Code quality (thermo-nuclear lane)

**Verdict: not approved as it stands.** Two of the rubric's blockers apply:
- **Incidental complexity a restructuring would delete:** G-1, with G-2.
- **Duplicated canonical helpers:** G-3, G-4.

No file is over 1,000 lines; the largest are `tests/test_chain.py` (638) and `flpcr/chain/driver.py`
(458). The driver is the file that grows with testnet signing and pushing. None of these remedies moves a
puzzle hash, except the optional G-7 `rotate` change.

- **G-1 (structural): the spend logic lives in the simulator, and `spend()` and `apply()` are split.**
  - What is there now:
    - the per-leaf solution layouts and auth messages live in `flpcr/chain/sim.py`;
    - `sim.py:221/238/248` has `isinstance(…, str)` branches;
    - `sim.py:232/251` builds `delegation_updates` by hand;
    - the caller hand-builds the slot and delegation updates that `Distributor.apply`
      (`driver.py:337`) needs, so it is not atomic, and a wrong dict silently diverges from the chain
      (also F-7);
    - an unknown slot is skipped with `continue` (`driver.py:320`).
  - Remedy:
    - a typed `Auth = BySignature | ByMessage | ByDelegate`;
    - per-leaf builders on `Distributor` returning `LeafCall`;
    - `prepare(calls) -> Prepared`, which carries spends, new state, slot and delegation updates,
      required messages and signatures, and unresolved slots;
    - `apply(prepared)`;
    - move `Keys` and `send_message_condition` (`driver.py:433/457`) to `sim.py`.
  - Size: `sim.py` about −90 lines, `driver.py` about +70, plus a new `auth.py` of about 40. Do this
    **before** `scripts/deploy_testnet.py`, which would otherwise copy it out of `sim.py`.
- **G-2: chain state is a positional CLVM value.** `read_state` (`driver.py:367`) decodes it by index
  into string-keyed dicts. Remedy: `chain/state.py` with frozen `ChainState`/`ChainCampaign`/`ChainRoot`
  (`to_value`, `from_program`), in `shared.rue` field order.
- **G-3: two config classes.** `distributor.Config` (line 32) validates; `driver.DistributorConfig`
  (line 82) doesn't, and it is the one the chain path uses. The simulator's defaults restate
  `Config.testnet`, and the differential test hand-maps the two and drops `max_campaigns`. Remedy: one
  `Config`.
- **G-4: the leaf list and compile logic are copied in three places, and the copies have diverged.**
  - The leaf list is in `contracts/build.py:34`, `driver.py:49` and `scripts/mutate.py`. Its order fixes
    every distributor's puzzle hash.
  - `mutate.py` has its own `build()` (line 33). It omits `finalizer` and fails on `"Error"`, which
    `build.py` doesn't.
  - Remedy: `mutate.py` imports `build.py`; the driver reads its leaf order from one source.
- **G-5: `links` is treated as optional by sniffing.**
  - `getattr(source, "links", lambda: [])()` at `engine.py:139`, then `self.links is None` branches at
    lines 158 and 173.
  - `Combined.__getattr__` forwarding in `chain/nfts.py`.
  - Remedy: `links()` is a required method, the `LinkBook` is always built (empty when there are no
    links), and the class is explicit.
- **G-6: "not posted" is a sentinel `Root(root=None)`** (`engine.py:52`). Remedy:
  `run_epoch -> (state, Optional[Root])`, so the root field itself is never `None`.
- **G-7: Rue struct literals are rebuilt field by field.**
  - `State` is rebuilt at six sites and `ReceiveMessage` at five.
  - Inline helpers (`receive_from`, `with_pending`, `with_claimed`) were **verified hash- and
    cost-neutral by compiling copies**.
  - `with_engine` in `rotate` shrinks the program (and moves its pin), so it is optional.
  - `init` and `promote` were not compiled with the helpers.
- **G-8: `Distributor.spend` (`driver.py:253`) is about 77 lines.** It mixes the action solution, the
  reserve spend and slot discovery, with bare opcodes (51, 66, mode 18). Remedy: split it into three
  functions and name the constants.
- **G-9: dead code and stale text.**
  - Dead code:
    - `distributor.claims_for` (line 264), never called;
    - `Distributor.claimable` (line 256), which mutates state as a query, never called;
    - `FixtureSource.load` (`sources.py:118`), never called.
  - Stale comments: `distributor.py` mentions a "phase 2"; `clvm.py` cites `tests/test_clvm.py`, which
    does not exist.
  - `sim.holder_message` is also used for delegates.
- **G-10: test duplication and a coverage gap.**
  - `EngineState(<launcher>, height=c.start)` is repeated about 29 times.
  - The seven hand-written `*Cat` subclasses (`test_chain.py:609–634`) **omit `TestAssertionProbes`**,
    so the create probes never run on the CAT deposit path.
  - Remedy: generate the CAT twins, so that any exclusion is written down.

## Verified safe

Each attack was run and refused for its own reason, with an honest control accepted in the same script.

**Authorization (A):** `p4_safe_battery.py` (52 checks) and `p5_revoke_same_spend.py`.
- Wrong-size and infinity public keys.
- Non-canonical `auth` and `grant` encodings.
- A message sent by a CAT coin at the holder's inner puzzle hash.
- Replays to an older singleton generation, and signatures reused across generations.
- Cross-leaf reuse of any message.
- Every wrong sender or prefix for `create`, `post_root`, `veto` and `rotate`.
- A delegate granting (one hop only).
- A revoke and a delegated claim in one spend with only the live slot.

**Slots (B):** `probe_slots.py`, 21/21 as expected.
- Equal-key re-insert.
- Stale neighbours.
- A fabricated `claimed`.
- The same key inserted twice in one spend.
- Forged claim and delegation slots.
- An evil-inner singleton parent.
- The byte-wise `>s` ordering, with top-bit keys.
- Nonce-1 and nonce-2 slots kept apart.
- Campaign id reuse.
- `init` is one-shot.

**Value (C):** the reserve invariant held throughout.
- Create bounds (`amount ≤ 0`, past `start`, `end ≤ start`, negative window, short deposit).
- Fee floor rounding.
- Claim arithmetic (cumulative 0, negative, inflated, repeated).
- The per-campaign cap, so no cross-campaign drain.
- `post_root` bounds.
- Claim then reclaim, and reclaim then claim.
- Reserve authority (a lone reserve spend, a swapped delegated puzzle).
- The CAT ring (a foreign asset, a siphon).
- A 0-mojo reserve.

**State machine (D):**
- No action can be early: `h` can't exceed the tip.
- A reclaim while a pending root names the campaign.
- Root sequencing.
- `init` front-running is harmless.
- A claim racing promotion.

**Engine (E):**
- CLVM integer and tree hashing (2,019 values, against `clvm` and the sdk).
- The merkle tree, for n = 1..69 and 255–257; single-leaf proofs; no internal node passes as a leaf.
- Exact `Fraction` accrual, and the `totals ≤ emittable` assertion.
- Parameter checks.
- The basic link rules.
- The router's XCH rule.

**Consistency (F):**
- Leaf order and selector formula.
- Tagged-condition order.
- Claim-leaf, campaign-id, reclaim and delegate field orders.
- Every doc default.
- The quoted test, differential and mutation numbers reproduce (166 tests; 47/77/44/14; 37/5/2/0).

**Measured cost (D, XCH, singleton CLVM cost / whole bundle, block limit 11e9):**

| Spend | Singleton CLVM cost | Whole bundle |
|---|---|---|
| create | 2.45M | 79.7M |
| post_root, 1 campaign | 2.76M | 81.2M |
| claim | 4.43M | 123.2M |
| post_root, 32 campaigns, with previous totals | 8.32M | 148.4M |
| claim, 32 campaigns in state | 5.46M | 172.5M (1.57% of a block) |

State size cannot brick the reserve.

## Coverage gaps

- **Testnet:** nothing was run on testnet11, and there is no bundle export.
- **Mempool:** the races (FLPCR-12, A-5) can't be modelled on the simulator.
- **Chained bundles:** no same-block bundle chained across two singleton generations was built; no two
  different-key inserts were chained on ephemeral slots.
- **CAT:** costs were measured only for XCH. `TestAssertionProbes` has no CAT twin.
- **Input lengths:** 0/31/33-byte `puzzle_hash`, `target` and `nonce` were not probed.
- **Live data:** there is no live `Source`, so CHIP-0020 hint spoofing on CAT holders is untested; NFT
  melt, DID recovery and deeper launchers were not exercised.
- **Sage:** the signing domain was not checked against `flpcr.links` or `AGG_SIG_ME`.
- **The differential suite** covers only message-authorized claims, creator reclaims and posts. It never
  runs delegates, signatures, veto, rotate, refused creates or posts, or multi-action spends.
- **Mutation:** `mutate.py` mutates `assert` lines only.

## Decisions needed

1. **FLPCR-04:** may anyone sweep a fully claimed campaign (it pays nothing), and/or a campaign past
   `end + 2 × claim_window` (paying the creator only)? Either relaxes "reclaim is creator-only". Also
   needed: the router's minimum budget and per-day rate (SPEC §11 Q1).
2. **FLPCR-06:** delayed operator rotation (a new `State` field), or document that the guardian is the
   root of trust for roots as well as vetoes.
3. **FLPCR-07:** should the creator's consent and funding come from the creator's own spend (mode 1
   message) instead of a "request nothing" offer?
4. **FLPCR-12:** a fee on `delegate` (a new `Config` field), or rely on operator retries.
5. **Ordering:** the puzzle fixes all move pins. Batch them into one re-pin (FLPCR-02, -03, -07, -08,
   -15, plus any decided above), then rerun the full suite, `mutate.py` and these probes against the new
   fingerprint. That is the runbook's completion gate.
