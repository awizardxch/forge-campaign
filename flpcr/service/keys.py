"""The host's own keys, made by the service and kept in its volume (owner, 2026-11-28: "add a new
self generated key with rotation and not reuse the same env").

A campaign host signs unattended: the operator posts a root every 8 hours on mainnet and the
router co-signs every create, at any hour, so their keys live where the service runs. With
FLPCR_KEY_FILE set and no key in the environment, the service makes both keys on its first start
and keeps them in that file (mode 600, in the service's volume). Nobody generates, copies or
pastes a key; the owner backs the file up (deploy/hetzner/README.md).

The file holds the router key and every operator key the host has had, oldest first:

    {"network": "mainnet", "router": "<hex>", "operators": ["<hex>", ...]}

Each is a synthetic secret key whose standard puzzle hash is the role's address, as
scripts/make_service_keys.py makes them. The newest operator launches new distributors; every
operator in the list still signs for the distributors whose engine it is, so a rotation never
strands one.

The owner may make the file on their own machine first (owner, 2026-11-28: "we should first
create local keys"), keep it as the backup, and copy it into the service's volume:

    python -m flpcr.service.keys --file <folder outside every repository>/mainnet.json --network mainnet create

Rotating the operator (a stolen or retired key):

    python -m flpcr.service.keys --file <file> new-operator

adds a fresh operator key and prints its address (never a secret). Then the guardian lock
proposes the rotation to that address on each distributor (it lands after `rotate_delay`, and the
admin lock may cancel it), and the site's pins.ts adds the new puzzle hash. The service signs with
whichever key each distributor's engine names, and moves the old address's XCH to the new one as
it posts.

The router is not rotated: every distributor is fixed at launch to its router. A new router key
means new distributors for new campaigns; the old ones keep their roots, claims and reclaims.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import sys
from dataclasses import dataclass
from typing import List, Optional

from chia_wallet_sdk import Address, SecretKey, standard_puzzle_hash

from .core import OperatorKey

PREFIX = {"mainnet": "xch", "testnet11": "txch"}


def fresh_key() -> SecretKey:
    """A fresh synthetic key, as scripts/make_service_keys.py makes one (not a wallet derivation)."""
    return SecretKey.from_seed(secrets.token_bytes(32)).derive_synthetic()


def _ph(sk: SecretKey) -> bytes:
    return standard_puzzle_hash(sk.public_key())


@dataclass
class KeyRing:
    network: str
    router: SecretKey
    operators: List[SecretKey]  # oldest first; the last launches new distributors

    @property
    def router_ph(self) -> bytes:
        return _ph(self.router)

    @property
    def operator(self) -> OperatorKey:
        """The newest operator: new distributors launch under it."""
        return OperatorKey(self.operators[-1])

    @property
    def all_operators(self) -> List[OperatorKey]:
        return [OperatorKey(sk) for sk in self.operators]

    def address(self, ph: bytes) -> str:
        return Address(ph, PREFIX[self.network]).encode()

    def public(self) -> dict:
        """Addresses and puzzle hashes only: what the site's pins and the router coin need."""
        ops = [_ph(sk) for sk in self.operators]
        return {"network": self.network,
                "router": {"address": self.address(self.router_ph), "puzzle_hash": self.router_ph.hex()},
                "operators": [{"address": self.address(p), "puzzle_hash": p.hex(), "launches": i == len(ops) - 1}
                              for i, p in enumerate(ops)]}

    def to_json(self) -> dict:
        return {"network": self.network, "router": self.router.to_bytes().hex(),
                "operators": [sk.to_bytes().hex() for sk in self.operators]}

    @classmethod
    def from_json(cls, body: dict) -> "KeyRing":
        ops = [SecretKey.from_bytes(bytes.fromhex(x)) for x in body.get("operators") or []]
        if not ops:
            raise ValueError("the key file names no operator key")
        return cls(str(body["network"]), SecretKey.from_bytes(bytes.fromhex(body["router"])), ops)


def _write(path: pathlib.Path, ring: KeyRing, replace: bool) -> None:
    """Written whole and only readable by the service's user: a temp file, then a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    if path.exists() and not replace:
        raise FileExistsError(f"{path} exists; a key file is never overwritten")
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(ring.to_json(), f, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load(path: pathlib.Path, network: str) -> Optional[KeyRing]:
    if not path.exists():
        return None
    ring = KeyRing.from_json(json.loads(path.read_text(encoding="utf-8")))
    if ring.network != network:
        raise ValueError(f"{path} holds {ring.network} keys, not {network}")
    return ring


def load_or_create(path: pathlib.Path, network: str, log=print) -> KeyRing:
    """The host's keys from `path`, made on first use. Logs only public halves."""
    ring = load(path, network)
    if ring is not None:
        return ring
    if network not in PREFIX:
        raise ValueError(f"unknown network {network!r}")
    ring = KeyRing(network, fresh_key(), [fresh_key()])
    _write(path, ring, replace=False)
    pub = ring.public()
    log(f"flpcr keys: made new keys in {path} (back this file up). router {pub['router']['address']}, "
        f"operator {pub['operators'][-1]['address']}")
    return ring


def new_operator(path: pathlib.Path, network: str) -> KeyRing:
    """Add a fresh operator key; it launches new distributors from the next start. Every older
    operator key stays, signing for the distributors whose engine it still is."""
    ring = load(path, network)
    if ring is None:
        raise FileNotFoundError(f"{path}: no key file")
    ring.operators.append(fresh_key())
    _write(path, ring, replace=True)
    return ring


def in_a_repository(path: pathlib.Path) -> bool:
    """Whether `path` would sit inside a git checkout (keys never go in one)."""
    here = path.resolve().parent
    return any((d / ".git").exists() for d in [here, *here.parents])


def create(path: pathlib.Path, network: str) -> KeyRing:
    """Make a new key file at `path`, outside every repository; never over an existing one."""
    if in_a_repository(path):
        raise ValueError(f"{path} is inside a git checkout; keys never go in one")
    if path.exists():
        raise FileExistsError(f"{path} exists; a key file is never overwritten")
    return load_or_create(path, network, log=lambda m: None)


def from_env(env, network: str, log=print) -> Optional[KeyRing]:
    """FLPCR_OPERATOR_SK and FLPCR_ROUTER_SK when set (testnet's hosts); else FLPCR_KEY_FILE, made
    on first use; else None (the service reads and claims only)."""
    op_hex = env.get("FLPCR_OPERATOR_SK", "").strip().removeprefix("0x")
    router_hex = env.get("FLPCR_ROUTER_SK", "").strip().removeprefix("0x")
    key_file = env.get("FLPCR_KEY_FILE", "").strip()
    if op_hex or router_hex:
        if key_file:
            raise ValueError("set FLPCR_KEY_FILE or the FLPCR_*_SK keys, not both")
        if not (op_hex and router_hex):
            return None  # partial: main() keeps its old behavior for each key on its own
        return KeyRing(network, SecretKey.from_bytes(bytes.fromhex(router_hex)),
                       [SecretKey.from_bytes(bytes.fromhex(op_hex))])
    if key_file:
        return load_or_create(pathlib.Path(key_file), network, log)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The campaign host's self-made keys. Prints public halves only.")
    ap.add_argument("--file", default=os.environ.get("FLPCR_KEY_FILE", ""), help="the key file (default: FLPCR_KEY_FILE)")
    ap.add_argument("--network", default=os.environ.get("FLPCR_NETWORK", ""), choices=sorted(PREFIX))
    ap.add_argument("command", choices=["create", "show", "new-operator"])
    args = ap.parse_args(argv)
    if not args.file:
        ap.error("--file or FLPCR_KEY_FILE is required")
    path = pathlib.Path(args.file)
    if args.command == "create":
        ring = create(path, args.network)
        print(f"Made {path}. It holds the secret keys: keep it as the backup, outside every repository, "
              "and copy it to the server's /srv/forge/state/flpcr-mainnet/keys/mainnet.json.", file=sys.stderr)
    elif args.command == "show":
        ring = load(path, args.network)
        if ring is None:
            print(f"{path}: no key file yet (the service makes one on its first start)", file=sys.stderr)
            return 1
    else:
        ring = new_operator(path, args.network)
        print("Added an operator key. Restart the service, have the guardian lock propose the rotation "
              "to the new address on each distributor, and add its puzzle hash to the site's pins.ts.",
              file=sys.stderr)
    print(json.dumps(ring.public(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
