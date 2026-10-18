# FLPCR — Forge Liquidity Pool Campaign Rewards

Incentive campaigns for Forge liquidity and Chia CATs. Anyone funds a reward budget on a Forge pool
(rewarding LP holders) or on a CAT (rewarding holders) over a block range, and pays a 3% fee out of
the deposit. An off-chain engine scores holders by time-weighted share and posts a cumulative claim
tree to an on-chain distributor singleton. Holders claim whenever they like. After the campaign ends
and the claim window the creator chose has passed, the creator may reclaim everything left
unclaimed; holders can keep claiming until that reclaim actually lands. The puzzle caps every
campaign at its emission schedule.

The reference design is Merkl's EVM incentive layer, rebuilt for Chia's coin set model on the
CHIP-0050 action layer that Forge V14 runs on. **Testnet only. Not audited.**

| | |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | The whole design: the Merkl mapping, campaign kinds, distributor, scoring, trust model, phases |
| [`docs/PUZZLES.md`](docs/PUZZLES.md) | Leaf-by-leaf build spec for the distributor (phase 2) |
| [`docs/API.md`](docs/API.md) | HTTP API, read and write |
| `flpcr/` | The engine (campaigns, exact time and fee weighting, forwarding, claim tree, manifests) and `distributor.py`, the executable reference for the on-chain rules |
| `tests/` | Engine and hashing tests; hashing is cross-checked against `clvm` when installed |

## Run

```bash
python -m pip install pytest clvm   # clvm is optional, it enables the cross-check
python -m pytest -q
python -m flpcr fixtures/demo.json --end 2000 --state /tmp/flpcr-state.json   # root 1
python -m flpcr fixtures/demo.json --end 3000 --state /tmp/flpcr-state.json   # root 2, cumulative
```

The engine itself is standard library only (Python 3.10+).

## Status

- **Phase 0 (here):** spec, engine, reference distributor model, tests.
- **Phase 1:** live chain source for testnet11 (hinted LP CAT coins, `forge_v14_resync` for fee events).
- **Phase 2:** distributor puzzles in Rue, in this repo under `contracts/`, with V14-style suites.
- **Phase 3:** operator / keeper / watcher bots, API, interface tab.
