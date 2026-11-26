"""Make a campaign host's two keys: the router (co-signs creates) and the operator (posts roots).

    python scripts/make_service_keys.py --network mainnet --out <a folder outside every repository>

Writes <out>/flpcr-<network>-router.json and <out>/flpcr-<network>-operator.json, each holding the
key's `synthetic_sk` (hex, what FLPCR_ROUTER_SK / FLPCR_OPERATOR_SK take), its address and puzzle
hash. Refuses to overwrite a file that is already there. Prints only the public half: the two
addresses and puzzle hashes, which the site's build pin (src/lib/campaigns/pins.ts) and the
router-coin funding need. The secrets go into the host's .env and a password manager, never into
a repository, a chat or a log.

The keys are fresh (not wallet derivations): a stolen server key is rotated by the guardian
(operator) or retired with its distributor (router); neither can reach a wallet.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import sys

from chia_wallet_sdk import Address, SecretKey, standard_puzzle_hash

PREFIX = {"mainnet": "xch", "testnet11": "txch"}


def make(network: str, out: pathlib.Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    made = {}
    paths = {role: out / f"flpcr-{network}-{role}.json" for role in ("router", "operator")}
    for path in paths.values():
        if path.exists():
            raise SystemExit(f"{path} exists; this script never overwrites a key")
    for role, path in paths.items():
        sk = SecretKey.from_seed(secrets.token_bytes(32)).derive_synthetic()
        ph = standard_puzzle_hash(sk.public_key())
        address = Address(ph, PREFIX[network]).encode()
        body = {"role": role, "network": network, "synthetic_sk": sk.to_bytes().hex(),
                "address": address, "puzzle_hash": ph.hex()}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f, indent=1)
            f.write("\n")
        made[role] = {"address": address, "puzzle_hash": ph.hex(), "file": str(path)}
    return made


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--network", required=True, choices=sorted(PREFIX))
    ap.add_argument("--out", required=True, help="a folder outside every repository")
    args = ap.parse_args()
    out = pathlib.Path(args.out).expanduser().resolve()
    here = pathlib.Path(__file__).resolve().parent.parent
    if here == out or here in out.parents:
        raise SystemExit("--out is inside this repository; keys never go in a checkout")
    made = make(args.network, out)
    for role, m in made.items():
        print(f"{role:9} {m['address']}  puzzle hash {m['puzzle_hash']}  -> {m['file']}")
    print("\nThe files hold the secret keys: copy each synthetic_sk into the host's .env "
          f"(FLPCR_{'MAINNET' if args.network == 'mainnet' else 'TESTNET'}_ROUTER_SK / _OPERATOR_SK) "
          "and a password manager. Share only the lines above.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
