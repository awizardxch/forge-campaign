# FLPCR — Forge Liquidity Pool Campaign Rewards

Incentive campaigns for Forge liquidity and Chia CATs. Anyone funds a reward budget on a Forge pool
(rewarding LP holders) or on a CAT (rewarding holders) over a block range, and pays a 3% fee out of
the deposit. An off-chain engine scores holders by time-weighted share and posts a cumulative claim
tree to an on-chain distributor singleton. Holders claim whenever they like. After the campaign ends
and the claim window the creator chose has passed, the creator may reclaim everything left
unclaimed; holders can keep claiming until that reclaim actually lands. The puzzle caps every
campaign at its emission schedule.

The reference design is Merkl's EVM incentive layer, rebuilt for Chia's coin set model on the
CHIP-0050 action layer that Forge V14 runs on. **Simulator-tested, not yet on testnet. Not audited.**

| | |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | The whole design: the Merkl mapping, campaign kinds, distributor, scoring, trust model, phases, and what the tests prove (§12) |
| [`docs/PUZZLES.md`](docs/PUZZLES.md) | The distributor puzzles as built: types, every leaf's rules, driver conventions |
| [`docs/API.md`](docs/API.md) | HTTP API, read and write |
| [`docs/TESTNET.md`](docs/TESTNET.md) | Local setup, and what is left before the first testnet11 campaign |
| `contracts/` | Rue project: upstream CHIP-0050 puzzles (pinned), the eight FLPCR leaves, `build.py`, `pins.json` |
| `flpcr/` | The engine (campaigns, exact time and fee weighting, forwarding, claim tree, manifests), `distributor.py` (the same rules as the puzzles, in Python), `router.py` (which campaigns our router signs) |
| `flpcr/links.py` | Address links: a wallet's addresses grouped for gates and boosts, both sides signed (CHIP-0002) |
| `flpcr/chain/` | Driver that builds distributor spends; `SimWorld`, a distributor on the chia-wallet-sdk simulator; `nfts.py`, NFT collections (by minter DID) and owners read from chain |
| `tests/` | Engine, reference model, router, chain (XCH and CAT), differential (model vs puzzles), gates and boosts, NFT collections on the simulator, address links |
| `scripts/mutate.py` | Mutation run: every leaf assertion must be pinned by a test or argued |

## Run

```bash
cargo install rue-cli --version 0.8.4 --locked
python -m pip install chia-wallet-sdk==0.36.0 pytest clvm
python contracts/build.py      # compile and check pins
python -m pytest -q            # 166 tests
python scripts/mutate.py       # optional, ~3 min

python -m flpcr fixtures/demo.json --end 2000 --state /tmp/flpcr-state.json   # engine: root 1
python -m flpcr fixtures/demo.json --end 3000 --state /tmp/flpcr-state.json   # root 2, cumulative
```

The engine on its own needs only the standard library (Python 3.10+). The chain tests skip if
`chia-wallet-sdk` is not installed.

## Status

- **Phase 0 ✅:** spec, engine, reference model, router rules.
- **Phase 2 ✅ on the simulator:** distributor puzzles, driver, chain, differential and mutation suites, for
  XCH and CAT rewards. Still open: the distributor registry, cost measurements, a second reader.
- **Phase 1 (next):** live chain source for testnet11 (hinted LP CAT coins, `forge_v14_resync` for fee
  events).
- **Phase 3:** deploy script with real keys, operator / watcher bots, API, interface tab. See
  [`docs/TESTNET.md`](docs/TESTNET.md).
