"""Address links: one holder, many addresses, for gates and boosts.

A Chia wallet spreads its coins over many addresses (puzzle hashes), so a
Wizard can sit at one address while the LP sits at another. Requirements are
checked per address, so that holder would not qualify. A link joins
addresses into one holder **for gates and boosts only**: holdings of
requirement assets are summed across the group, and every address in it is
judged by the sum. Rewards are still credited to, and paid to, the address
that holds the tracked asset.

Messages (v2), signed in Chia's `signMessageByAddress` format (CHIP-0002,
UTF-8 mode: the key signs sha256tree(("Chia Signed Message", message)) with
the address's synthetic key; an address belongs to a key when
standard_puzzle_hash(synthetic key) == address):

    flpcr-link-v2:<distributor_id>:<primary>:<member>:<seq>     both addresses sign
    flpcr-unlink-v2:<distributor_id>:<primary>:<member>:<seq>   either address signs

The distributor id scopes a signature to one distributor. `seq` is a counter
per pair of addresses, shared by links and unlinks: an entry is accepted only
with a `seq` above every earlier signed entry for the same pair, so an old
link cannot be replayed to undo a later unlink. Either side can unlink.

The log. The operator keeps one append-only log per distributor and records
each entry at the current peak height when it is submitted. An entry's
`height` is that recording height: it is not signed, so it is bound by the
log instead. Heights never decrease along the log; every root manifest
commits sha256 of the log prefix with heights below the root's `end` (see
`log_hash`), and the engine refuses to build a root if the log rewrites a
prefix an earlier root committed or appends an entry dated before that
root's end. So no link can reach back into a window a posted root settled;
inside the open window the recorded height is the operator's word, and the
published log shows it.

Rules, applied in log order:
  * A link takes effect at its `height` and lasts until replaced or unlinked.
  * An address is in at most one group. Linking it again moves it.
  * Groups are one level deep: a primary cannot be a member of another group,
    and an address that is a primary for others cannot become a member.
  * A group has at most MAX_MEMBERS members besides its primary.
  * An unlink of (primary, member) takes the member out of that primary's
    group; it does nothing if the member is elsewhere.
A signed entry that breaks a rule is ignored, not an error, but still uses up
its `seq`. An entry that fails verification is ignored and uses up nothing.

What a link cannot stop: two people who both sign can pool their holdings,
so one Wizard can boost a friend's address. That is consent, and it is
visible in the published log.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

from .clvm import sha256tree

LINK_TAG = "flpcr-link-v2"
UNLINK_TAG = "flpcr-unlink-v2"
MAX_MEMBERS = 16


def link_message(distributor_id: bytes, primary: bytes, member: bytes, seq: int, unlink: bool = False) -> str:
    tag = UNLINK_TAG if unlink else LINK_TAG
    return f"{tag}:{distributor_id.hex()}:{primary.hex()}:{member.hex()}:{seq}"


def signed_digest(message: str) -> bytes:
    """What `signMessageByAddress` signs for a UTF-8 message."""
    return sha256tree(("Chia Signed Message", message))


@dataclass(frozen=True)
class Link:
    distributor_id: bytes
    primary: bytes
    member: bytes
    seq: int  # per pair, strictly increasing along the log
    height: int  # where the operator recorded it in the log
    unlink: bool = False
    # A link carries both signatures; an unlink at least one of them.
    member_pk: bytes = b""  # synthetic public key of `member`
    member_sig: bytes = b""
    primary_pk: bytes = b""
    primary_sig: bytes = b""

    @property
    def message(self) -> str:
        return link_message(self.distributor_id, self.primary, self.member, self.seq, self.unlink)

    @property
    def pair(self) -> Tuple[bytes, bytes]:
        """The `seq` counter's key: the two addresses, in either role."""
        return (self.primary, self.member) if self.primary < self.member else (self.member, self.primary)


def _signed_by(address: bytes, pk_bytes: bytes, sig_bytes: bytes, message: str) -> bool:
    try:
        from chia_wallet_sdk import PublicKey, Signature, standard_puzzle_hash
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("verifying links needs chia-wallet-sdk") from e
    if not pk_bytes or not sig_bytes:
        return False
    try:
        pk = PublicKey.from_bytes(pk_bytes)
        sig = Signature.from_bytes(sig_bytes)
    except Exception:
        return False
    if pk.to_bytes() == PublicKey.infinity().to_bytes():
        return False  # the identity key "signs" anything with the identity signature
    return standard_puzzle_hash(pk) == address and pk.verify(signed_digest(message), sig)


def verify(link: Link) -> bool:
    """Signatures and addresses only; the log rules (seq, shape) are LinkBook's."""
    if link.primary == link.member or link.seq < 1:
        return False
    message = link.message
    member_ok = _signed_by(link.member, link.member_pk, link.member_sig, message)
    if link.unlink and member_ok:
        return True
    primary_ok = _signed_by(link.primary, link.primary_pk, link.primary_sig, message)
    return primary_ok if link.unlink else member_ok and primary_ok


def check_log(links: Iterable[Link]) -> List[Link]:
    """The log as a list, refusing one whose heights go backwards: that is the
    operator rewriting history, never a user's mistake."""
    out = list(links)
    for a, b in zip(out, out[1:]):
        if b.height < a.height:
            raise ValueError(f"link log out of height order at height {b.height}")
    return out


class LinkBook:
    """Who is grouped with whom, at any height, for one distributor."""

    def __init__(
        self,
        links: Iterable[Link],
        distributor_id: bytes,
        check_signatures: bool = True,
        max_members: int = MAX_MEMBERS,
    ):
        self.max_members = max_members
        # (height, member -> primary) after every height at which something changed.
        self._snapshots: List[Tuple[int, Dict[bytes, bytes]]] = []
        last_seq: Dict[Tuple[bytes, bytes], int] = {}
        current: Dict[bytes, bytes] = {}
        for link in check_log(links):
            if link.distributor_id != distributor_id:
                continue
            if link.seq <= last_seq.get(link.pair, 0):
                continue  # a replay, or out of order
            if check_signatures and not verify(link):
                continue
            last_seq[link.pair] = link.seq
            nxt = self._apply(current, link)
            if nxt is None:
                continue
            current = nxt
            if self._snapshots and self._snapshots[-1][0] == link.height:
                self._snapshots[-1] = (link.height, current)
            else:
                self._snapshots.append((link.height, current))

    def _apply(self, current: Dict[bytes, bytes], link: Link) -> Optional[Dict[bytes, bytes]]:
        if link.primary == link.member:
            return None
        nxt = dict(current)
        if link.unlink:
            if current.get(link.member) != link.primary:
                return None
            del nxt[link.member]
            return nxt
        nxt.pop(link.member, None)
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


def sign(synthetic_sk, distributor_id: bytes, primary: bytes, member: bytes, seq: int,
         unlink: bool = False) -> Tuple[bytes, bytes]:
    """(public key, signature) for one side of a link or unlink, from an
    address's synthetic secret key: what a wallet's signMessageByAddress
    returns. For tests and tooling; a wallet signs for real users."""
    sig = synthetic_sk.sign(signed_digest(link_message(distributor_id, primary, member, seq, unlink)))
    return synthetic_sk.public_key().to_bytes(), sig.to_bytes()


def link_to_json(link: Link) -> dict:
    """The published form. Everything needed to re-verify the entry is here."""
    return {
        "distributor_id": link.distributor_id.hex(),
        "primary": link.primary.hex(),
        "member": link.member.hex(),
        "seq": link.seq,
        "height": link.height,
        "unlink": link.unlink,
        "message": link.message,
        "member_pk": link.member_pk.hex(),
        "member_sig": link.member_sig.hex(),
        "primary_pk": link.primary_pk.hex(),
        "primary_sig": link.primary_sig.hex(),
    }


def link_from_json(d: dict) -> Link:
    h = bytes.fromhex
    return Link(
        h(d["distributor_id"]), h(d["primary"]), h(d["member"]), d["seq"], d["height"], bool(d.get("unlink", False)),
        h(d.get("member_pk", "")), h(d.get("member_sig", "")), h(d.get("primary_pk", "")), h(d.get("primary_sig", "")),
    )


def log_to_json(links: Iterable[Link]) -> List[dict]:
    """The publishable log: what a watcher needs to replay link-dependent roots."""
    return [link_to_json(link) for link in links]


def log_hash(links: Iterable[Link]) -> bytes:
    """sha256 of the canonical JSON of a log (prefix); root manifests carry it."""
    canonical = json.dumps(log_to_json(links), sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).digest()


EMPTY_LOG_HASH = log_hash([])
