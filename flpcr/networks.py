"""Where each network's fixed addresses come from.

A distributor curries `dev_fee_ph` (the 3% creation fee goes there) and
`admin_ph` (may reclaim a campaign for its creator and cancel a pending
operator rotation) for its whole life. Both are the dev multisig.

* testnet11: a default address is fine to keep in code; FLPCR_DEV_FEE_ADDRESS
  overrides it.
* mainnet: never in code. The address is read from the environment variable
  the Forge site already uses for its dev fee, VITE_CFMM_POOL_DEV_FEE_ADDRESS_MAINNET,
  and launching refuses without it.

FLPCR_ADMIN_ADDRESS overrides the admin on either network; by default the
admin is the dev fee address.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

TESTNET_DEV_FEE_ADDRESS = "txch1zm9z9h4fjn88qa7f82a9n5lga2qyydd8l53jqw9qm2z5mh0ge6kqwt5jps"
MAINNET_DEV_FEE_ENV = "VITE_CFMM_POOL_DEV_FEE_ADDRESS_MAINNET"

PREFIXES = {"testnet11": "txch", "mainnet": "xch"}


class AddressError(ValueError):
    pass


def decode(address: str, network: str) -> bytes:
    """The puzzle hash of `address`, which must be on `network`."""
    from chia_wallet_sdk import Address

    try:
        decoded = Address.decode(address.strip())
    except Exception as e:  # the SDK raises its own error types
        raise AddressError(f"not a Chia address: {address!r}") from e
    if decoded.prefix != PREFIXES[network]:
        raise AddressError(f"{address} is a {decoded.prefix} address, not {PREFIXES[network]} ({network})")
    return bytes(decoded.puzzle_hash)


@dataclass(frozen=True)
class NetworkAddresses:
    network: str
    dev_fee_address: str
    admin_address: str

    @property
    def dev_fee_ph(self) -> bytes:
        return decode(self.dev_fee_address, self.network)

    @property
    def admin_ph(self) -> bytes:
        return decode(self.admin_address, self.network)


def addresses(network: str, env: Optional[Mapping[str, str]] = None) -> NetworkAddresses:
    env = os.environ if env is None else env
    if network not in PREFIXES:
        raise AddressError(f"unknown network {network!r}; expected one of {sorted(PREFIXES)}")
    if network == "mainnet":
        dev = env.get(MAINNET_DEV_FEE_ENV, "").strip()
        if not dev:
            raise AddressError(f"mainnet needs {MAINNET_DEV_FEE_ENV}; the dev multisig address is never kept in code")
    else:
        dev = env.get("FLPCR_DEV_FEE_ADDRESS", "").strip() or TESTNET_DEV_FEE_ADDRESS
    admin = env.get("FLPCR_ADMIN_ADDRESS", "").strip() or dev
    out = NetworkAddresses(network, dev, admin)
    out.dev_fee_ph, out.admin_ph  # decode now, so a bad address fails before anything is built
    return out
