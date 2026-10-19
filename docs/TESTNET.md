# FLPCR — Getting to testnet11

> For running this repo on a local machine. Everything in this repo today runs on the in-process
> simulator. This page covers what works now, what has to be built before the first testnet11
> campaign, and the order to build it in.

## 1. Local setup (works today)

```bash
git clone https://github.com/awizardxch/forge-campaign.git
cd forge-campaign
git checkout claude/practical-keller-xjj6zd     # until it is merged

# Rue compiler (Rust toolchain needed); 0.8.4 is Forge's version
cargo install rue-cli --version 0.8.4 --locked

python -m venv .venv
.venv/bin/pip install chia-wallet-sdk==0.36.0 pytest clvm   # Windows: .venv\Scripts\pip

python contracts/build.py          # compiles, checks every pin
python -m pytest -q                # 166 tests, ~4 s
python scripts/mutate.py           # optional, ~3 min: every assertion is load-bearing
```

The engine alone (`flpcr/` minus `flpcr/chain/`) needs only the standard library.

## 2. What testnet needs that the simulator does not

| Piece | Simulator today | Testnet11 |
|---|---|---|
| Dev fee address | a test puzzle (`dev`) | The Speechless dev multisig's puzzle hash, curried as `dev_fee_ph` at launch (fixed for the distributor's life) |
| Router, operator, guardian keys | anyone-can-spend test puzzles (`Keys` in `flpcr/chain/driver.py`) | Router and operator: a standard wallet key each, signing the `SEND_MESSAGE` coin (Sage or a script). Guardian: a Forge multisig safe (`forgeMultisig.md`) |
| Pushing spends | `Simulator.spend_coins` | A full node RPC `push_tx`, or coinset.org. The driver already returns plain `CoinSpend`s. Only signing and pushing are new |
| Chain data for the engine | `FixtureSource` | A live `Source` (`flpcr/sources.py` lists what it must return): hinted LP CAT coins and spends by asset id, and fee events from `forge_resync` (dispatches by protocol: V16 = 17, V15 = 16, V14 = 15) |
| NFT holdings for gates and boosts | `ChainNftSource` reads the simulator (`flpcr/chain/nfts.py`) | The same code against a node: it needs three calls, `coin_state`, `children`, `coin_spend`, which map to `get_coin_record_by_name`, `get_coin_records_by_parent_ids` and `get_puzzle_and_solution`. A thin adapter, then point it at the Wizard DID |
| Address links | verified in tests with generated keys | Sage's `signMessageByAddress`. Check one real Sage signature against `flpcr.links.verify` first |
| Finding the distributor again | the driver holds it in memory | Persist the launcher id plus the driver's state (state value, slot list, lineage). Or rebuild it by replaying the singleton's spends, the way `forge_v16_resync` rebuilds a pool |
| Fees | none | The network fee comes from the offer or the operator's coin. The bundle already tolerates extra XCH (it goes to the fee) |
| Clocks | `Config.testnet`-style: 8-block disputes, 16-block minimum campaign | Same numbers work on testnet11 (~2.5 min disputes, ~5 min campaigns); production 38 / 77 |

## 3. Suggested order

1. **Deploy script** (`scripts/deploy_testnet.py`): launch one XCH distributor and one for a registry CAT
   with real keys, run `init`, and save the launcher ids and driver state to a local JSON file (never
   committed).
2. **One manual campaign.** Create through a Sage offer (give `amount`, request nothing) plus the router's
   message; post a hand-built root; claim; reclaim after a 16-block window. This is `TestLifecycle`, run on
   testnet.
3. **Live Source** for the engine against every live Forge pool (V16, plus the V15 and V14 pools kept live), listed from the registries; check that two replays from scratch produce
   identical roots.
4. **Operator bot.** Post a root whenever the previous one is active, and post the root covering a
   campaign's `end` within a few blocks of it.
5. **Watcher.** Replay each pending root and alert or veto on a mismatch.
6. **API and the forge-ui tab** (`API.md`, SPEC §9).

## 4. Things to keep in mind

- **Slack.** Conservation is bundle-wide. A bundle with spare XCH will quietly fund a short XCH deposit
  from that spare. That is safe (the reserve is always whole) but it can surprise whoever paid the fee.
- **Slot spends.** Two claims in one spend must not touch the same slot. The driver does not yet split
  conflicting claims across spends; send claims one per spend, or for holders whose keys are not
  neighbours.
- **The router message is per singleton coin.** A create whose bundle is built against a stale
  singleton coin fails, because the message names a coin that is already spent. Rebuild against the
  current coin.
