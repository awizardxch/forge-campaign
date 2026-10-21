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
CHIP-0050 action layer that Forge V16 runs on (the same pinned upstream puzzles). **Simulator-tested and running on testnet11 (five distributors so far). Not audited.**

| | |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | The whole design: the Merkl mapping, campaign kinds, distributor, scoring, trust model, phases, and what the tests prove (§12) |
| [`docs/PUZZLES.md`](docs/PUZZLES.md) | The distributor puzzles as built: types, every leaf's rules, driver conventions |
| [`docs/API.md`](docs/API.md) | HTTP API, read and write |
| [`docs/AGENT_TOOLS.md`](docs/AGENT_TOOLS.md) | The `flpcr.*` agent tools: inputs, outputs, who signs, checks, and the code behind each |
| [`docs/TESTNET.md`](docs/TESTNET.md) | Local setup, the testnet11 distributors and what each proved, `deploy_testnet.py` commands and guards, what is left |
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
python -m pytest tests -q      # 378 tests (Windows: .venv/Scripts/python.exe)
python scripts/mutate.py       # optional, ~3 min

python -m flpcr fixtures/demo.json --end 2000 --state /tmp/flpcr-state.json   # engine: root 1
python -m flpcr fixtures/demo.json --end 3000 --state /tmp/flpcr-state.json   # root 2, cumulative
```

The engine on its own needs only the standard library (Python 3.10+). The chain tests skip if
`chia-wallet-sdk` is not installed.

## Status

- **Pre-testnet review (2026-10-19):** [`reviews/2026-10-19/FINDINGS.md`](reviews/2026-10-19/FINDINGS.md). It found five High
  findings, none a drain, plus 12 more. The on-chain fixes (FLPCR-02 to -08 and -12 to -16, and the
  driver restructure) are applied in one re-pin of every leaf; the off-chain ones (engine, links, NFT
  reader) are on a separate branch. Its probes still need a rerun against the new pins.
- **Phase 0 ✅:** spec, engine, reference model, router rules.
- **Phase 2 ✅ on the simulator:** distributor puzzles (ten leaves: init, create, post_root, veto, claim,
  reclaim, rotate, delegate, dispute, resolve), driver, chain, differential and mutation suites, for
  XCH and CAT rewards. Still open: the distributor registry, cost measurements beyond claims (claims: about 38.7M cost each,
  about 125 per bundle), a second reader.
- **Excluded addresses and NFT campaigns (2026-10-20):** the burn address and up to 16 creator-excluded
  addresses are refused at claim; `NFT_HOLD` pays one share per NFT, by minter DID or `col1` collection.
  Proven on testnet11 (fourth and fifth distributors, `docs/TESTNET.md` §5). 378 tests.
- **Phase 1 (in progress):** live LP holders of every V16 pool are built (`LiveLpSource`, time share
  only), and NFT holders read from a node (`NodeReader`). Still open: `forge_resync` fee events and the
  V15 and V14 pools.
- **Phase 3 (in progress):** `scripts/deploy_testnet.py` with real keys runs the distributor on testnet11.
  Still open: operator / watcher bots, API, interface tab. See [`docs/TESTNET.md`](docs/TESTNET.md).
