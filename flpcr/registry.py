"""The distributor registry: how a wallet, the site or an agent finds a distributor cold.

A registry is a JSON file per network (`registry/<network>.json`), committed in this
repo and served by the service at `GET /api/v1/distributors`. It is an index, not an
authority: every entry is checked against the chain before anything relies on it.

    key      (reward asset, router_ph): at most one "current" distributor per key.
             A newer one for the same key retires the older (a re-pin means a new
             distributor; holders of the old one still claim there until reclaimed).
    verify   resync the launcher with the leaves of THIS build. That alone proves the
             distributor runs the current pins (resync rebuilds the Config from the
             first spend and refuses any other leaves). Then the reward asset, router,
             guardian, admin and dev fee address on chain must be the entry's.

Why a file and not a singleton: every distributor today is ours, launched by one
router key, so an on-chain registry would add a puzzle to review and prove nothing
the chain check does not already prove. A second operator is the reason to build one
(docs/SPEC.md §4: key = (reward asset, router_ph), sorted slots).

    python -m flpcr.registry list   --network testnet11
    python -m flpcr.registry add    --network testnet11 --launcher <hex> [--api <url>] [--note <text>]
    python -m flpcr.registry verify --network testnet11        # exit 1 if any current entry fails

`add` verifies before it writes; `--node` overrides the network's default node.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

ROOT = pathlib.Path(__file__).resolve().parent.parent
REGISTRY_DIR = ROOT / "registry"
NETWORKS = ("testnet11", "mainnet")
CURRENT, RETIRED = "current", "retired"
ROLES = ("reward_asset", "router_ph", "guardian_ph", "admin_ph", "dev_fee_ph")


class RegistryError(ValueError):
    pass


def _hex32(name: str, v: str) -> str:
    v = str(v).strip().lower().removeprefix("0x")
    if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
        raise RegistryError(f"{name} must be 32 bytes of hex")
    return v


def pins_fingerprint(compiled: Optional[pathlib.Path] = None) -> str:
    """`(cd contracts/compiled && sha256sum *.hex | sha256sum)`: the build a distributor runs."""
    from .chain.driver import COMPILED  # the folder the driver loads (FLPCR_COMPILED aware)

    folder = pathlib.Path(compiled or COMPILED)
    lines = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in sorted(folder.glob("*.hex")))
    return hashlib.sha256(lines.encode()).hexdigest()


@dataclass
class Entry:
    launcher_id: str
    reward_asset: str  # 00…00 is XCH
    router_ph: str
    guardian_ph: str
    admin_ph: str
    dev_fee_ph: str
    pins: str  # pins_fingerprint() of the build it was launched with
    status: str = CURRENT
    api: Optional[str] = None  # base URL of the service that serves it, if any
    note: str = ""

    def __post_init__(self) -> None:
        self.launcher_id = _hex32("launcher_id", self.launcher_id)
        for name in ROLES:
            setattr(self, name, _hex32(name, getattr(self, name)))
        if self.status not in (CURRENT, RETIRED):
            raise RegistryError(f"status must be {CURRENT} or {RETIRED}")
        if len(self.pins) != 64:
            raise RegistryError("pins must be a sha256 fingerprint")
        if self.api is not None and not self.api.startswith(("https://", "http://")):
            raise RegistryError("api must be an http(s) URL")

    @property
    def key(self) -> Tuple[str, str]:
        return (self.reward_asset, self.router_ph)


@dataclass
class Registry:
    network: str
    entries: List[Entry] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.network not in NETWORKS:
            raise RegistryError(f"network must be one of {NETWORKS}")
        seen, live = set(), set()
        for e in self.entries:
            if e.launcher_id in seen:
                raise RegistryError(f"launcher {e.launcher_id[:12]}… is listed twice")
            seen.add(e.launcher_id)
            if e.status == CURRENT:
                if e.key in live:
                    raise RegistryError(f"two current distributors for reward asset {e.reward_asset[:12]}… "
                                        f"and router {e.router_ph[:12]}…")
                live.add(e.key)

    # --- reads ---------------------------------------------------------------------------

    def get(self, launcher_id: str) -> Optional[Entry]:
        lid = _hex32("launcher_id", launcher_id)
        return next((e for e in self.entries if e.launcher_id == lid), None)

    def current(self, reward_asset: Optional[str] = None, router_ph: Optional[str] = None) -> List[Entry]:
        """Current distributors, optionally for one reward asset and/or one router."""
        asset = reward_asset and _hex32("reward_asset", reward_asset)
        router = router_ph and _hex32("router_ph", router_ph)
        return [e for e in self.entries if e.status == CURRENT
                and (asset is None or e.reward_asset == asset) and (router is None or e.router_ph == router)]

    # --- writes --------------------------------------------------------------------------

    def add(self, entry: Entry) -> Optional[Entry]:
        """List a distributor as current; the current one for the same key, if any, is
        retired and returned. A launcher is listed once: re-adding it is refused."""
        if self.get(entry.launcher_id) is not None:
            raise RegistryError(f"launcher {entry.launcher_id[:12]}… is already listed")
        if entry.status != CURRENT:
            raise RegistryError("add lists a current distributor")
        old = next((e for e in self.entries if e.status == CURRENT and e.key == entry.key), None)
        if old is not None:
            old.status = RETIRED
        self.entries.append(entry)
        return old

    def retire(self, launcher_id: str) -> Entry:
        e = self.get(launcher_id)
        if e is None:
            raise RegistryError("unknown launcher")
        e.status = RETIRED
        return e

    # --- file ----------------------------------------------------------------------------

    def to_json(self) -> dict:
        return {"network": self.network, "version": 1, "distributors": [asdict(e) for e in self.entries]}

    @classmethod
    def from_json(cls, raw: dict) -> "Registry":
        if raw.get("version") != 1:
            raise RegistryError("unknown registry version")
        return cls(raw["network"], [Entry(**e) for e in raw.get("distributors", [])])

    @classmethod
    def load(cls, network: str, folder: Optional[pathlib.Path] = None) -> "Registry":
        path = pathlib.Path(folder or REGISTRY_DIR) / f"{network}.json"
        if not path.exists():
            return cls(network)
        reg = cls.from_json(json.loads(path.read_text()))
        if reg.network != network:
            raise RegistryError(f"{path.name} is for {reg.network}")
        return reg

    def save(self, folder: Optional[pathlib.Path] = None) -> pathlib.Path:
        path = pathlib.Path(folder or REGISTRY_DIR) / f"{self.network}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=1) + "\n")
        return path


# --- the chain check -----------------------------------------------------------------------

def entry_from_chain(reader, launcher_id: bytes, puzzles=None, api: Optional[str] = None, note: str = "") -> Entry:
    """Resync the launcher with this build's leaves and read the entry off its Config.
    Raises ResyncError if the distributor does not run these leaves."""
    from chia_wallet_sdk import Clvm

    from .chain.driver import Puzzles
    from .chain.resync import resync

    got = resync(reader, puzzles or Puzzles(Clvm()), launcher_id)
    cfg = got.config
    return Entry(launcher_id.hex(), cfg.reward_asset.hex(), cfg.router_ph.hex(), cfg.guardian_ph.hex(),
                 cfg.admin_ph.hex(), cfg.dev_fee_ph.hex(), pins_fingerprint(), CURRENT, api, note)


def verify_entry(reader, entry: Entry, puzzles=None) -> Optional[str]:
    """None if the chain agrees with a current entry; otherwise why not."""
    from .chain.resync import ResyncError

    if entry.pins != pins_fingerprint():
        return f"listed with pins {entry.pins[:12]}…, this build is {pins_fingerprint()[:12]}…"
    try:
        seen = entry_from_chain(reader, bytes.fromhex(entry.launcher_id), puzzles)
    except ResyncError as e:
        return f"does not replay with this build's leaves ({e})"
    except Exception as e:  # noqa: BLE001  (an unknown launcher, a node that does not answer)
        return f"could not be read from chain ({type(e).__name__}: {e})"
    for name in ROLES:
        if getattr(seen, name) != getattr(entry, name):
            return f"{name} on chain is {getattr(seen, name)[:12]}…, listed as {getattr(entry, name)[:12]}…"
    return None


def verify(reader, registry: Registry, puzzles=None) -> Dict[str, Optional[str]]:
    """Every current entry against the chain: launcher id -> None, or the reason it fails.
    Retired entries are not checked: they run older leaves, which this build cannot replay."""
    return {e.launcher_id: verify_entry(reader, e, puzzles) for e in registry.current()}


# --- CLI -------------------------------------------------------------------------------------

def _reader(network: str, node: Optional[str]):
    from .chain.node_reader import NodeReader
    from .service.main import NODES

    return NodeReader.connect(node or NODES[network])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m flpcr.registry", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "add", "verify", "retire"):
        p = sub.add_parser(name)
        p.add_argument("--network", required=True, choices=NETWORKS)
        p.add_argument("--dir", default=None, help="registry folder (default: registry/)")
        p.add_argument("--node", default=None)
        if name in ("add", "retire"):
            p.add_argument("--launcher", required=True)
        if name == "add":
            p.add_argument("--api", default=None)
            p.add_argument("--note", default="")
    a = ap.parse_args(argv)
    reg = Registry.load(a.network, a.dir)
    if a.cmd == "list":
        for e in reg.entries:
            asset = "XCH" if e.reward_asset == "00" * 32 else e.reward_asset[:12] + "…"
            print(f"{e.status:8} {e.launcher_id}  reward {asset}  pins {e.pins[:12]}…  {e.api or '-'}  {e.note}")
        return 0
    if a.cmd == "retire":
        reg.retire(a.launcher)
        print(f"retired; wrote {reg.save(a.dir)}")
        return 0
    reader = _reader(a.network, a.node)
    if a.cmd == "add":
        entry = entry_from_chain(reader, bytes.fromhex(_hex32("launcher", a.launcher)), api=a.api, note=a.note)
        old = reg.add(entry)
        print(f"listed {entry.launcher_id[:12]}… as current" + (f"; retired {old.launcher_id[:12]}…" if old else ""))
        print(f"wrote {reg.save(a.dir)}")
        return 0
    bad = 0
    for lid, why in verify(reader, reg).items():
        print(f"{'ok  ' if why is None else 'FAIL'} {lid}" + (f"  {why}" if why else ""))
        bad += why is not None
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RegistryError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
