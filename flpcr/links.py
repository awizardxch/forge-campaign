"""Address links: one holder, many addresses, for gates and boosts.

A Chia wallet spreads its coins over many addresses (puzzle hashes), so a
Wizard can sit at one address while the LP sits at another. Requirements are
checked per address, so that holder would not qualify. A link joins
addresses into one holder **for gates and boosts only**: holdings of
requirement assets are summed across the group, and every address in it is
judged by the sum. Rewards are still credited to, and paid to, the address
that holds the tracked asset.

A link is signed by **both** addresses, each with its own key, over the same
message. A one-sided link would let anyone attach their LP address to someone
else's Wizard. Signatures use Chia's `signMessageByAddress` format (CHIP-0002,
UTF-8 mode): the key signs sha256tree(("Chia Signed Message", message)) with
the address's synthetic key, and an address belongs to a key when
standard_puzzle_hash(synthetic key) == address. Anyone can re-verify a link
from what is published, so roots stay reproducible.

Rules, applied in (height, member, primary) order:
  * A link takes effect at its `height` (when the operator recorded it; it is
    published with the manifests) and lasts until replaced.
  * An address is in at most one group. Linking it again moves it.
  * Groups are one level deep: a primary cannot be a member of another group,
    and an address that is a primary for others cannot become a member.
  * A group has at most MAX_MEMBERS members besides its primary.
  * An unlink is a link with member == primary, signed by that address
    alone: it leaves whatever group it was in.
Anything that breaks a rule, or fails verification, is ignored, not an error.

What a link cannot stop: two people who both sign can pool their holdings,
so one Wizard can boost a friend's address. That is consent, and it is
visible in the published links.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

from .clvm import sha256tree

LINK_TAG = "flpcr-link-v1"
MAX_MEMBERS = 16


def link_message(primary: bytes, member: bytes) -> str:
    return f"{LINK_TAG}:{primary.hex()}:{member.hex()}"


def signed_digest(message: str) -> bytes:
    """What `signMessageByAddress` signs for a UTF-8 message."""
    return sha256tree(("Chia Signed Message", message))


@dataclass(frozen=True)
class Link:
    primary: bytes
    member: bytes
    height: int
    member_pk: bytes  # synthetic public key of `member`
    member_sig: bytes
    primary_pk: bytes = b""  # unused for an unlink
    primary_sig: bytes = b""

    @property
    def is_unlink(self) -> bool:
        return self.primary == self.member


def _signed_by(address: bytes, pk_bytes: bytes, sig_bytes: bytes, message: str) -> bool:
    try:
        from chia_wallet_sdk import PublicKey, Signature, standard_puzzle_hash
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("verifying links needs chia-wallet-sdk") from e
    try:
        pk = PublicKey.from_bytes(pk_bytes)
        sig = Signature.from_bytes(sig_bytes)
    except Exception:
        return False
    return standard_puzzle_hash(pk) == address and pk.verify(signed_digest(message), sig)


def verify(link: Link) -> bool:
    message = link_message(link.primary, link.member)
    if not _signed_by(link.member, link.member_pk, link.member_sig, message):
        return False
    return link.is_unlink or _signed_by(link.primary, link.primary_pk, link.primary_sig, message)


class LinkBook:
    """Who is grouped with whom, at any height."""

    def __init__(self, links: Iterable[Link], check_signatures: bool = True, max_members: int = MAX_MEMBERS):
        self.max_members = max_members
        # (height, member -> primary) after every height at which something changed.
        self._snapshots: List[Tuple[int, Dict[bytes, bytes]]] = []
        current: Dict[bytes, bytes] = {}
        for link in sorted(links, key=lambda l: (l.height, l.member, l.primary)):
            if check_signatures and not verify(link):
                continue
            nxt = self._apply(current, link)
            if nxt is None:
                continue
            current = nxt
            if self._snapshots and self._snapshots[-1][0] == link.height:
                self._snapshots[-1] = (link.height, current)
            else:
                self._snapshots.append((link.height, current))

    def _apply(self, current: Dict[bytes, bytes], link: Link) -> Optional[Dict[bytes, bytes]]:
        nxt = dict(current)
        nxt.pop(link.member, None)
        if link.is_unlink:
            return nxt if link.member in current else None
        if link.primary in nxt:  # the primary is itself a member somewhere
            return None
        if any(p == link.member for p in nxt.values()):  # the member is a primary for others
            return None
        if sum(1 for p in nxt.values() if p == link.primary) >= self.max_members:
            return None
        nxt[link.member] = link.primary
        return nxt

    def _at(self, height: int) -> Dict[bytes, bytes]:
        mapping: Dict[bytes, bytes] = {}
        for h, snap in self._snapshots:
            if h > height:
                break
            mapping = snap
        return mapping

    def group(self, address: bytes, height: int) -> FrozenSet[bytes]:
        mapping = self._at(height)
        primary = mapping.get(address, address)
        return frozenset({primary, *(m for m, p in mapping.items() if p == primary)})

    def changes(self, start: int, end: int) -> List[int]:
        return [h for h, _ in self._snapshots if start < h < end]


def sign(synthetic_sk, primary: bytes, member: bytes) -> Tuple[bytes, bytes]:
    """(public key, signature) for one side of a link, from an address's
    synthetic secret key: what a wallet's signMessageByAddress returns. For
    tests and tooling; a wallet signs for real users."""
    sig = synthetic_sk.sign(signed_digest(link_message(primary, member)))
    return synthetic_sk.public_key().to_bytes(), sig.to_bytes()


def link_to_json(link: Link) -> dict:
    """The published form. Everything needed to re-verify the link is here."""
    return {
        "primary": link.primary.hex(),
        "member": link.member.hex(),
        "height": link.height,
        "message": link_message(link.primary, link.member),
        "member_pk": link.member_pk.hex(),
        "member_sig": link.member_sig.hex(),
        "primary_pk": link.primary_pk.hex(),
        "primary_sig": link.primary_sig.hex(),
    }


def link_from_json(d: dict) -> Link:
    h = bytes.fromhex
    return Link(h(d["primary"]), h(d["member"]), d["height"], h(d["member_pk"]), h(d["member_sig"]),
                h(d.get("primary_pk", "")), h(d.get("primary_sig", "")))
