"""How an action is authorized, and what a bundle must carry for it.

A leaf call names an `Auth`; the driver turns it into the leaf's `auth,
auth_data` solution fields and into the `Message`s and `Signature`s the bundle
must supply. Whoever pushes the bundle (the simulator, a wallet, a deploy
script) satisfies those; the driver never signs or spends anything itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

# shared.rue
AUTH_SIGNATURE = 0
AUTH_MESSAGE = 1
AUTH_DELEGATE = 2
AUTH_ADMIN = 3
# CHIP-0025 message modes: sender by puzzle hash, receiver by coin id (every
# action but claims), or receiver uncommitted (claims: generation-free).
MODE_PUZZLE_TO_COIN = 23
MODE_PUZZLE_TO_ANY = 16


@dataclass(frozen=True)
class BySignature:
    """The address's standard-wallet synthetic key signs: AGG_SIG_ME against
    the singleton coin, or for a claim AGG_SIG_UNSAFE over the claim leaf."""

    public_key: bytes


@dataclass(frozen=True)
class ByMessage:
    """A coin at the address is spent and sends "k" + message to the singleton."""


@dataclass(frozen=True)
class ByDelegate:
    """A delegate the owner granted authorizes, itself by signature or message."""

    delegate: bytes
    via: Union[BySignature, ByMessage] = ByMessage()


@dataclass(frozen=True)
class ByAdmin:
    """Reclaim only: a coin at the distributor's admin_ph sends "k" + message."""


Auth = Union[BySignature, ByMessage, ByDelegate, ByAdmin]


@dataclass(frozen=True)
class Message:
    """A coin at `sender` must send `message` (prefix included): to the
    singleton coin (mode 23), or for a claim to no committed receiver (mode 16)."""

    sender: bytes
    message: bytes
    mode: int = MODE_PUZZLE_TO_COIN


@dataclass(frozen=True)
class Signature:
    """`public_key` must sign `message`: AGG_SIG_ME against the singleton coin,
    or with `unsafe` AGG_SIG_UNSAFE over the message alone (claims)."""

    public_key: bytes
    message: bytes
    unsafe: bool = False
