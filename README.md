# FLPCR — Forge Liquidity Pool Campaign Rewards

Incentive campaigns for Forge liquidity, Chia CATs and NFT collections. Anyone funds a reward budget
on a Forge pool (rewarding LP holders), on a CAT (rewarding holders) or on an NFT collection (one share
per NFT, nothing staked) over a block range, and pays a 3% fee out of the deposit. The burn address
never earns, and a creator can exclude up to 16 addresses; the claim leaf enforces both. An off-chain engine scores holders by time-weighted share and posts a cumulative claim
tree to an on-chain distributor singleton. Holders claim whenever they like, by key signature (a wallet
signs for all its addresses in one approval, no coin at any of them) or by a coin spend from the address;
no claim names a generation of the distributor, so a sequencer pays many holders in one bundle of
chained generations. After the campaign ends
and the claim window the creator chose has passed, everything left unclaimed can be reclaimed: by
the creator, the creator's delegate, or the dev multisig acting as admin, and always paid to the
creator. Holders can keep claiming until that reclaim actually lands. The puzzle caps every
campaign at its emission schedule. Every posted root waits out a dispute window, in which the guardian
can veto it and **anyone can dispute it with a bond** (Merkl's dispute, as a coin: 5 XCH in production,
0.1 TXCH on testnet); the guardian or the dev multisig resolves, refunding the bond if the dispute was
right and forfeiting it to the dev multisig if it was not.

The reference design is Merkl's EVM incentive layer, rebuilt for Chia's coin set model on the
CHIP-0050 action layer that Forge V16 runs on (the same pinned upstream puzzles). **Simulator-tested and running on testnet11 (eight distributors so far, three on the current pins), with the service hosted beside the Forge's testnet router and the forge.awizard.dev 🪎 tab reading it. Not audited; not on mainnet.**

| | |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | The whole design: the Merkl mapping, campaign kinds, distributor, scoring, trust model, phases, and what the tests prove (§12) |
| [`docs/PUZZLES.md`](docs/PUZZLES.md) | The distributor puzzles as built: types, every leaf's rules, driver conventions |
| [`docs/API.md`](docs/API.md) | HTTP API, read and write |
| [`docs/AGENT_TOOLS.md`](docs/AGENT_TOOLS.md) | The `flpcr.*` agent tools: inputs, outputs, who signs, checks, and the code behind each |
| [`docs/TESTNET.md`](docs/TESTNET.md) | Local setup, the testnet11 distributors and what each proved, `deploy_testnet.py` commands and guards, what is left |
| [`docs/SAGE_CHECKS.md`](docs/SAGE_CHECKS.md) | The launch checklist that needs a real wallet, ticked with on-chain evidence |
| [`docs/FORGE_SITE_TAB.md`](docs/FORGE_SITE_TAB.md) | The 🪎 tab's contract: what the site reads and what the wallet signs |
| `flpcr/service/` | The hosted service: operator (posts roots), claim sequencer, create intake, watcher, HTTP API (`docs/API.md`); `Dockerfile` builds it |
| `registry/` | The chain-verified distributor registry, one file per network |
| `contracts/` | Rue project: upstream CHIP-0050 puzzles (pinned), the ten FLPCR leaves plus the bond launcher, `build.py`, `pins.json` |
| `flpcr/` | The engine (campaigns, exact time and fee weighting, forwarding, claim tree, manifests), `distributor.py` (the same rules as the puzzles, in Python, and the one `Config`), `leaves.py` (the one leaf list), `router.py` (which campaigns our router signs) |
| `flpcr/links.py` | Address links: a wallet's addresses grouped for gates and boosts, both sides signed (CHIP-0002) |
| `flpcr/lp_crawl.py`, `flpcr/live_source.py` | Live LP holders: every holder of each V16 pool's LP CAT, crawled from the pool's LP mint-eve puzzle hash forward (custody excluded, incremental, cached in the gitignored `lp-crawl-<network>.json`); `LiveLpSource` serves them to the engine, with LP asset ids from the Forge responder |
| `flpcr/chain/` | `driver.py` builds distributor spends (per-leaf `LeafCall`s, `prepare`, `apply`, and `sequence` for many claims in one bundle); `auth.py` typed authorizations; `state.py` typed chain state; `sim.py` `SimWorld`, a distributor on the chia-wallet-sdk simulator; `nfts.py`, NFT collections (by minter DID, or a `col1` collection by hash-checked metadata) and owners read from chain; `node_reader.py`, the same three chain calls from a full node or coinset.org |
| `tests/` | Engine, reference model, router, chain (XCH, with a generated CAT twin of every class), differential (model vs puzzles), gates and boosts, NFT collections and NFT campaigns on the simulator, excluded addresses, address links, the LP crawl, the reclaim guard |
| `scripts/deploy_testnet.py`, `scripts/dashboard.py` | testnet11: launch, create (`--exclude`, `--kind nft`), post (`--source live`), claim, claim-all, guarded reclaim; and a local dev dashboard that signs through the Sage RPC |
| `scripts/mutate.py` | Mutation run: every leaf assertion and emitted guard must be pinned by a test or argued |
| `scripts/scale_claims.py` | Claims at scale on the simulator: 5,000 holders, each claiming by key signature, all paid exactly in 42 bundles (94 generations), about 36 minutes of blocks |

## Run

```bash
cargo install rue-cli --version 0.8.4 --locked
python -m pip install chia-wallet-sdk==0.36.0 pytest clvm
python contracts/build.py      # compile and check pins
python -m pytest tests -q      # 492 tests (Windows: .venv/Scripts/python.exe)
python scripts/mutate.py       # optional, ~3 min

python -m flpcr fixtures/demo.json --end 2000 --state /tmp/flpcr-state.json   # engine: root 1
python -m flpcr fixtures/demo.json --end 3000 --state /tmp/flpcr-state.json   # root 2, cumulative
```

The engine on its own needs only the standard library (Python 3.10+). The chain tests skip if
`chia-wallet-sdk` is not installed.

## Status

- **Pre-testnet review (2026-10-19):** [`reviews/2026-10-19/FINDINGS.md`](reviews/2026-10-19/FINDINGS.md): five High
  findings, none a drain, plus 12 more; the on-chain fixes are in one re-pin of every leaf. The 2026-11-09
  rerun (`reviews/2026-10-19/rerun/`) refused 16 of 17 probes, and found FLPCR-08 short and three new dispute-bond and reclaim findings (NEW-1 to NEW-3); those are fixed in the
  `fix-bond-reclaim` re-pin, and NEW-4 (bond launcher bound to the pending root) after it. Distributors
  launched before that re-pin keep the old leaves (`docs/TESTNET.md` §5).
- **Phase 0 ✅:** spec, engine, reference model, router rules.
- **Phase 2 ✅ on the simulator:** distributor puzzles (ten leaves: init, create, post_root, veto, claim,
  reclaim, rotate, delegate, dispute, resolve), driver, chain, differential and mutation suites, for XCH and
  CAT rewards. The distributor registry is a chain-verified file (`registry/<network>.json`,
  `python -m flpcr.registry`). Claims cost about 38.7M each, about 125 per bundle.
- **Excluded addresses and NFT campaigns ✅ (2026-10-20):** the burn address and up to 16 creator-excluded
  addresses are refused at claim; `NFT_HOLD` pays one share per NFT, by minter DID or `col1` collection.
- **Phase 1 (in progress):** live LP holders and fee events of every Forge V16 pool are built
  (`LiveLpSource`, `flpcr/v16_fees.py`; older pools are refused), and NFT holders are read from a node.
  Fee-weighted campaigns are not yet run on live testnet data (SAGE_CHECKS B3).
- **Phase 3 (testnet, 2026-11-10):**
  - Current distributors: `85b5d8e7…` (XCH rewards; its operator was rotated to the service key) and
    `31ad2dca…` (T6 CAT rewards), both on the current pins (`registry/testnet11.json`).
  - [`docs/SAGE_CHECKS.md`](docs/SAGE_CHECKS.md): A3 to A5, B1, C2, C6 and C7 are done with on-chain
    evidence. The service posted roots and paid a claim on chain. Still open: A1 (address links) and A2
    (claims signed over WalletConnect from the tab), B2 (lock claim), B3 (fee-weighted), C3 (several
    claimants in one batch), C4 (watcher alert), C5 (create intake, waiting on a router key).
  - The service answers reads while an operator post waits to confirm, takes one request for every
    address of a wallet (`POST /api/v1/claimable`), and accepts one combined signature over the claim
    leaves and the fee spend, which is what one wallet prompt returns.
  - Hosting: the Forge's Hetzner kit (forge-ui `deploy/hetzner`) builds this repository's `main` as the
    `flpcr-testnet` container, served at the testnet router's domain under `/flpcr/`. Its keys are set
    on the server only.
  - The 🪎 tab on forge.awizard.dev (Forge PR 355): pair rows, a page per pair at `/rewards/<lp name>`,
    Claim, Participate and Create. For now it is shown to admin and dev wallets only.
- **Mainnet: not started.** It waits on the SAGE_CHECKS list, an external review, a multisig guardian, and
  the owner's open decisions. No mainnet step runs without the owner.
