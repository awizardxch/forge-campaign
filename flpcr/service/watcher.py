"""The watcher (`flpcr.watch`): replay each pending root and act before it activates.

    python -m flpcr.service.watcher --state state-testnet11.json --once
    python -m flpcr.service.watcher --launcher <hex> --network testnet11 --role disputer \\
        --disputer txch1... --manifests ./manifests --webhook https://hooks.example/flpcr

A check is one pass:

1. `resync` rebuilds the distributor from chain (reused while the singleton is unspent).
2. Every posted root the chain still stands on is replayed with the engine from scratch,
   with the same `Source` the service uses, over the windows the chain recorded. The
   pending root's replay is compared field by field with what `post_root` put on chain:
   root id, start, end, root, every campaign's total and the manifest hash. If the root
   commits a manifest (a nonzero `manifest_hash`) the published manifest is fetched and
   checked too: its hash, its fields, its tree (its leaves must rebuild to its root), its
   link-log commitment, and every holder's cumulative against the replay's.
3. The verdict is one of

   * `MATCH`: everything replays. A pending root with a committed manifest that cannot be
     fetched, or no manifest at all under `require_manifest`, is *not* a match.
   * `MISMATCH`: with the exact diff (fields, campaigns, holders where a manifest gives the
     posted leaves).
   * `UNVERIFIABLE`: the replay could not run (source data missing, a campaign the engine
     cannot rebuild, an empty window) or the manifest is missing. Never treated as a match:
     it alerts, and prepares no remedy.
   * `NO_PENDING`: nothing to check.

4. On a mismatch the watcher prepares, but never signs or pushes, the remedy for its role
   through the driver (`LeafCall` / `prepare`):

   * `disputer` (anyone else's watcher): `dispute`, with `Prepared.creations` (the bond
     launcher the disputer's own coin must create; the coin is picked from the disputer's
     address) and the launcher's spend already in `Prepared.spends`.
   * `guardian`: `veto` (message `"v" + sha256tree(root_id)` from `guardian_ph`). A veto is
     refused while a dispute is open, so with one open the guardian's remedy is `resolve`:
     upheld (1) on a mismatch, rejected (0) on a match.
   * `admin`: `resolve` only (the admin cannot veto).
   * `observer`: alerts only.

   A pending root activates on the first spend at or after `activates_at`, so a remedy must
   land before then; `blocks_remaining` says how long is left, and at zero no remedy is
   prepared.

The alert payload (`Report.alert()`) is JSON for a webhook. The watcher holds no key.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Callable, Dict, List, Optional, Sequence

from chia_wallet_sdk import Clvm, Coin

from ..chain.driver import HEIGHT_WINDOW, Prepared, Puzzles
from ..chain.resync import Resynced, resync
from ..engine import EngineState, Root, run_epoch
from ..manifest import manifest_hash, root_to_json
from ..merkle import build_tree, claim_leaf
from ..model import Campaign
from .core import campaigns_from, chain_root_ids

MATCH, MISMATCH, UNVERIFIABLE, NO_PENDING = "MATCH", "MISMATCH", "UNVERIFIABLE", "NO_PENDING"
ROLES = ("observer", "disputer", "guardian", "admin")
ZERO = bytes(32).hex()
MAX_ALERT_HOLDERS = 50

ManifestLookup = Callable[[int, str], Optional[dict]]  # (root id, manifest hash hex) -> manifest JSON


# --- reports ----------------------------------------------------------------------------


@dataclass
class Remedy:
    """What the role may send, prepared and unsigned. `prepared`'s programs belong to the
    watcher's own `Clvm`: sign and push its spends (or `to_json()`), and apply it only to a
    driver built from the same `puzzles_factory`."""

    action: str  # "dispute" | "veto" | "resolve"
    h: int
    prepared: Optional[Prepared] = None
    upheld: Optional[int] = None  # resolve only
    disputer_coin: Optional[Coin] = None  # dispute only: the coin that must create `creations`
    refused: Optional[str] = None  # why nothing was prepared
    note: Optional[str] = None

    def to_json(self) -> dict:
        out: dict = {"action": self.action, "h": self.h, "valid_before_height": self.h + HEIGHT_WINDOW}
        if self.upheld is not None:
            out["upheld"] = self.upheld
        if self.refused:
            out["refused"] = self.refused
        if self.note:
            out["note"] = self.note
        p = self.prepared
        if p is not None:
            out["spends"] = [{"coin": {"parent_coin_info": s.coin.parent_coin_info.hex(),
                                       "puzzle_hash": s.coin.puzzle_hash.hex(), "amount": s.coin.amount},
                              "puzzle_reveal": s.puzzle_reveal.hex(), "solution": s.solution.hex()} for s in p.spends]
            out["messages"] = [{"sender": m.sender.hex(), "mode": m.mode, "message": m.message.hex()} for m in p.messages]
            out["creations"] = [{"puzzle_hash": ph.hex(), "amount": amt, "memo": memo.hex(),
                                 "from_coin": self.disputer_coin.coin_id().hex() if self.disputer_coin else None}
                                for ph, amt, memo in p.creations]
            out["signatures_needed"] = len(p.signatures)
            out["reserved_fee"] = p.fee
        return out


@dataclass
class Report:
    status: str
    network: str
    launcher_id: str
    peak: int
    role: str
    root_id: Optional[int] = None
    posted: Optional[dict] = None
    expected: Optional[dict] = None
    diff: List[dict] = field(default_factory=list)  # [{"field", "posted", "expected"}]
    campaigns: List[dict] = field(default_factory=list)  # [{"campaign", "posted", "expected"}]
    holders: Optional[List[dict]] = None  # [{"campaign", "puzzle_hash", "posted", "expected"}]; None: no posted leaves
    reasons: List[str] = field(default_factory=list)  # why UNVERIFIABLE
    notes: List[str] = field(default_factory=list)
    activates_at: Optional[int] = None
    blocks_remaining: Optional[int] = None
    dispute: Optional[dict] = None
    rotation: Optional[dict] = None
    earlier: List[dict] = field(default_factory=list)  # earlier roots that did not replay
    remedy: Optional[Remedy] = None

    @property
    def should_alert(self) -> bool:
        return (self.status in (MISMATCH, UNVERIFIABLE) or self.dispute is not None or self.rotation is not None
                or bool(self.earlier))

    def to_json(self) -> dict:
        out = {k: v for k, v in self.__dict__.items() if k != "remedy"}
        out["remedy"] = self.remedy.to_json() if self.remedy else None
        return out

    def key(self) -> str:
        """Identifies what an alert is about, so a loop sends each one once."""
        return json.dumps([self.status, self.root_id, self.posted and self.posted.get("root"),
                           self.dispute, self.rotation, [e["root_id"] for e in self.earlier]], sort_keys=True)

    def alert(self) -> dict:
        """The webhook payload: what is wrong, how long is left, what was prepared."""
        holders = self.holders or []
        summary = {MATCH: "the pending root replays", NO_PENDING: "no root pending",
                   MISMATCH: "the pending root does not replay", UNVERIFIABLE: "the pending root could not be checked"}
        text = f"root {self.root_id}: {summary[self.status]}"
        if self.blocks_remaining is not None and self.status != NO_PENDING:
            text += f"; {self.blocks_remaining} blocks before it activates" if self.dispute is None else "; frozen by a dispute"
        return {
            "type": "flpcr.watch", "network": self.network, "launcher_id": self.launcher_id, "peak": self.peak,
            "status": self.status, "text": text, "root_id": self.root_id, "activates_at": self.activates_at,
            "blocks_remaining": self.blocks_remaining, "diff": self.diff, "campaigns": self.campaigns,
            "holders": holders[:MAX_ALERT_HOLDERS], "holders_total": len(holders), "reasons": self.reasons,
            "notes": self.notes, "dispute": self.dispute, "rotation": self.rotation, "earlier": self.earlier,
            "remedy": self.remedy and {k: v for k, v in self.remedy.to_json().items() if k != "spends"},
        }


# --- comparing ----------------------------------------------------------------------------


def _root_fields(root: Root) -> dict:
    return {"root_id": root.root_id, "start": root.start, "end": root.end, "root": root.root.hex(),
            "totals": {cid.hex(): amt for cid, amt in sorted(root.totals.items())},
            "manifest_hash": manifest_hash(root).hex(),
            "links": {"count": root.link_count, "sha256": root.links_hash.hex()}}


def _canonical_hash(manifest: dict) -> str:
    return sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _totals_diff(posted: Dict[str, int], expected: Dict[str, int]) -> List[dict]:
    return [{"campaign": cid, "posted": posted.get(cid), "expected": expected.get(cid)}
            for cid in sorted(set(posted) | set(expected)) if posted.get(cid) != expected.get(cid)]


def _holders_diff(manifest: dict, expected: Root) -> List[dict]:
    posted = {(c["campaign_id"], c["puzzle_hash"]): c["cumulative"] for c in manifest.get("claims", [])}
    want = {(c.campaign_id.hex(), c.puzzle_hash.hex()): c.cumulative for c in expected.claims}
    return [{"campaign": cid, "puzzle_hash": ph, "posted": posted.get((cid, ph)), "expected": want.get((cid, ph))}
            for cid, ph in sorted(set(posted) | set(want)) if posted.get((cid, ph)) != want.get((cid, ph))]


def _manifest_tree_root(distributor_id: bytes, manifest: dict) -> Optional[str]:
    leaves = [claim_leaf(distributor_id, bytes.fromhex(c["campaign_id"]), bytes.fromhex(c["puzzle_hash"]), c["cumulative"])
              for c in manifest.get("claims", [])]
    if not leaves:
        return None
    root, _ = build_tree(leaves)
    return root.hex()


def compare(posted: dict, expected: Root, manifest: Optional[dict]) -> tuple:
    """(diff, campaigns, holders) between what the chain (and the manifest) say and the replay."""
    want = _root_fields(expected)
    diff: List[dict] = []
    for key in ("root_id", "start", "end", "root"):
        if posted[key] != want[key]:
            diff.append({"field": key, "posted": posted[key], "expected": want[key]})
    campaigns = _totals_diff(posted["totals"], want["totals"])
    if campaigns:
        diff.append({"field": "totals", "posted": posted["totals"], "expected": want["totals"]})
    if posted["manifest_hash"] != ZERO and posted["manifest_hash"] != want["manifest_hash"]:
        diff.append({"field": "manifest_hash", "posted": posted["manifest_hash"], "expected": want["manifest_hash"]})
    holders = None
    if manifest is not None:
        got = _canonical_hash(manifest)
        if posted["manifest_hash"] != ZERO and got != posted["manifest_hash"]:
            diff.append({"field": "manifest.file_hash", "posted": posted["manifest_hash"], "expected": got,
                         "note": "the published file is not the one the root committed"})
        for key in ("root_id", "start", "end", "root"):
            if manifest.get(key) != posted[key]:
                diff.append({"field": f"manifest.{key}", "posted": manifest.get(key), "expected": posted[key],
                             "note": "the manifest disagrees with the chain"})
        if manifest.get("totals") != posted["totals"]:
            diff.append({"field": "manifest.totals", "posted": manifest.get("totals"), "expected": posted["totals"],
                         "note": "the manifest disagrees with the chain"})
        tree = _manifest_tree_root(expected.distributor_id, manifest)
        if tree != manifest.get("root"):
            diff.append({"field": "manifest.tree", "posted": manifest.get("root"), "expected": tree,
                         "note": "the manifest's leaves do not build its root"})
        if manifest.get("links") != want["links"]:
            diff.append({"field": "manifest.links", "posted": manifest.get("links"), "expected": want["links"]})
        holders = _holders_diff(manifest, expected)
        if holders and not any(d["field"] == "root" for d in diff):
            diff.append({"field": "manifest.claims", "posted": len(manifest.get("claims", [])),
                         "expected": len(expected.claims)})
    return diff, campaigns, holders


# --- manifests ----------------------------------------------------------------------------


def manifest_lookup(spec: Optional[str]) -> Optional[ManifestLookup]:
    """A directory (`<hash>.json` or `root-<id>.json` in it) or a URL template with `{hash}`
    and/or `{root_id}` (a bare URL prefix means `<prefix>/{hash}.json`). Missing: None."""
    if not spec:
        return None
    if spec.startswith(("http://", "https://")):
        template = spec if "{" in spec else spec.rstrip("/") + "/{hash}.json"

        def fetch(root_id: int, hash_hex: str) -> Optional[dict]:
            url = template.format(hash=hash_hex, root_id=root_id)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "flpcr-watcher/1.0"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                raise
        return fetch
    folder = pathlib.Path(spec)

    def read(root_id: int, hash_hex: str) -> Optional[dict]:
        for name in (f"{hash_hex}.json", f"root-{root_id}.json"):
            path = folder / name
            if path.is_file():
                return json.loads(path.read_text())
        return None
    return read


def publish_manifest(folder: pathlib.Path, root: Root) -> pathlib.Path:
    """Write a root's manifest where `manifest_lookup(folder)` finds it (operator side, and tests)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{manifest_hash(root).hex()}.json"
    path.write_text(json.dumps(root_to_json(root), sort_keys=True, separators=(",", ":")))
    return path


# --- the watcher --------------------------------------------------------------------------


class Watcher:
    def __init__(self, chain, launcher_id: bytes, source_for: Callable[[Sequence[Campaign]], object],
                 network: str = "testnet11", role: str = "observer", disputer_ph: Optional[bytes] = None,
                 manifests: Optional[ManifestLookup] = None, require_manifest: bool = False,
                 disputer_fee: int = 0, puzzles_factory=None):
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if role == "disputer" and disputer_ph is None:
            raise ValueError("a disputer watcher needs the disputer's address (where the bond is refunded from)")
        self.chain = chain
        self.launcher_id = launcher_id
        self.source_for = source_for
        self.network = network
        self.role = role
        self.disputer_ph = disputer_ph
        self.manifests = manifests
        self.require_manifest = require_manifest
        self.disputer_fee = disputer_fee  # what the disputer's coin must hold beyond the bond
        self.puzzles_factory = puzzles_factory or (lambda: Puzzles(Clvm()))
        self._got: Optional[Resynced] = None
        self._clvm: Optional[Clvm] = None
        self.last: Optional[Report] = None  # the latest check, for the service's /api/v1/watch

    # chain -------------------------------------------------------------------------------

    def resynced(self) -> Resynced:
        """The distributor from chain; resynced again only once its singleton coin has been spent."""
        if self._got is not None and self.chain.reader.coin_spend(self._got.distributor.coin.coin_id()) is None:
            return self._got
        puzzles = self.puzzles_factory()
        self._got = resync(self.chain.reader, puzzles, self.launcher_id)
        self._clvm = puzzles.clvm
        return self._got

    # replay ------------------------------------------------------------------------------

    def _replay(self, got: Resynced, through: int, report: Report) -> Optional[Root]:
        """Every root through `through` from scratch, over the chain's windows; the last one's
        replay, or None (the reasons are in the report)."""
        d = got.distributor
        try:
            campaigns = campaigns_from(got, self._clvm)
        except Exception as e:  # noqa: BLE001
            report.reasons.append(f"the campaigns could not be rebuilt from chain ({type(e).__name__}: {e})")
            return None
        if not campaigns:
            report.reasons.append("no campaigns on chain")
            return None
        live = {c.id for c in d.chain_state.campaigns}
        engine = EngineState(d.launcher_id, height=min(c.start for c in campaigns.values()))
        ids = [rid for rid in chain_root_ids(got) if int(rid) <= through]
        if not ids or int(ids[-1]) != through:
            report.reasons.append(f"root {through}'s post_root is not among the replayed spends")
            return None
        expected = None
        for rid in ids:
            posted = got.roots[rid]
            # What the operator should have passed: every campaign live when it posted. A campaign
            # in the totals was; one live now was too, or accrues nothing before its start.
            ids_in = {bytes.fromhex(k) for k in posted["totals"]} | live
            covered = [campaigns[c] for c in sorted(ids_in) if c in campaigns]
            try:
                engine, root = run_epoch(engine, covered, self.source_for(covered), posted["end"])
            except Exception as e:  # noqa: BLE001
                report.reasons.append(f"root {rid}: the engine could not replay it ({type(e).__name__}: {e})")
                return None
            if root is None:
                report.reasons.append(f"root {rid}: the engine finds nothing claimable in [.., {posted['end']}): "
                                      "the source is missing data, or the root was made up")
                return None
            if int(rid) < through and root.root.hex() != posted["root"]:
                report.earlier.append({"root_id": int(rid), "posted": posted["root"], "expected": root.root.hex()})
            expected = root
        return expected

    # check -------------------------------------------------------------------------------

    def check(self) -> Report:
        got = self.resynced()
        d = got.distributor
        st = d.chain_state
        h = self.chain.peak()
        report = Report(NO_PENDING, self.network, self.launcher_id.hex(), h, self.role)
        if st.rotation is not None:
            report.rotation = {"engine_ph": st.rotation.engine_ph.hex(), "effective_at": st.rotation.effective_at}
            report.notes.append("an operator rotation is proposed: if it is not expected, the admin cancels it "
                                "before effective_at")
        pending = st.pending
        if pending is None:
            if st.active is not None:  # still replay what is claimable, for the record
                self._replay(got, st.active.root_id, report)
                report.reasons.clear()
            self.last = report
            return report
        report.root_id = pending.root_id
        report.activates_at = pending.activates_at
        report.blocks_remaining = pending.activates_at - h
        if st.dispute is not None:
            report.dispute = {"root_id": st.dispute.root_id, "disputer_ph": st.dispute.disputer_ph.hex(),
                              "bond_parent": st.dispute.bond_parent.hex()}
        rec = got.roots.get(str(pending.root_id), {})
        posted = {"root_id": pending.root_id, "start": pending.start, "end": pending.end, "root": pending.root.hex(),
                  "totals": {cid.hex(): amt for cid, amt in pending.totals},
                  "manifest_hash": rec.get("manifest_hash", ZERO) if rec.get("root") == pending.root.hex() else ZERO,
                  "activates_at": pending.activates_at}
        report.posted = posted

        manifest = None
        if posted["manifest_hash"] != ZERO:
            if self.manifests is None:
                report.reasons.append("the root commits a manifest, and no manifest source is configured")
            else:
                try:
                    manifest = self.manifests(pending.root_id, posted["manifest_hash"])
                except Exception as e:  # noqa: BLE001
                    report.reasons.append(f"the manifest could not be fetched ({type(e).__name__}: {e})")
                if manifest is None and not any("manifest" in r for r in report.reasons):
                    report.reasons.append(f"the committed manifest {posted['manifest_hash'][:16]}… is not published")
        elif self.require_manifest:
            report.reasons.append("the root commits no manifest (manifest_hash is zero) and one is required")
        else:
            report.notes.append("the root commits no manifest: checked by replay alone")

        expected = self._replay(got, pending.root_id, report)
        if expected is None:
            report.status = UNVERIFIABLE
        else:
            report.expected = _root_fields(expected)
            report.diff, report.campaigns, report.holders = compare(posted, expected, manifest)
            if report.diff:
                report.status = MISMATCH
                if report.holders is None:
                    report.notes.append("which holders differ needs the posted leaves, which only its manifest has")
            elif report.reasons:
                report.status = UNVERIFIABLE
            else:
                report.status = MATCH
        report.remedy = self._remedy(report, h)
        self.last = report
        return report

    # remedies ----------------------------------------------------------------------------

    def _remedy(self, report: Report, h: int) -> Optional[Remedy]:
        d = self._got.distributor
        st = d.chain_state
        disputed = st.dispute is not None and st.dispute.root_id == report.root_id
        if self.role == "observer" or report.status in (NO_PENDING, UNVERIFIABLE):
            return None
        if disputed:
            if self.role not in ("guardian", "admin"):
                return None  # already disputed: the guardian or admin settles it
            upheld = 1 if report.status == MISMATCH else 0
            sender = d.cfg.guardian_ph if self.role == "guardian" else d.cfg.admin_ph
            note = ("veto is refused while a dispute is open; resolve upheld revokes the root and refunds the bond"
                    if upheld else "the disputed root replays: reject the dispute (the bond goes to the dev multisig "
                    "and the window restarts)")
            return self._prepare("resolve", h, lambda: [d.resolve(h, upheld, sender=sender)], upheld=upheld, note=note)
        if report.status != MISMATCH:
            return None
        if report.blocks_remaining is not None and report.blocks_remaining <= 0:
            return Remedy("veto" if self.role == "guardian" else "dispute", h,
                          refused="too late: the root activates on the next spend of the distributor")
        if self.role == "guardian":
            return self._prepare("veto", h, lambda: [d.veto(h, report.root_id)],
                                 note=f"must confirm before height {report.activates_at}")
        if self.role == "admin":
            return Remedy("dispute", h, refused="the admin can neither veto nor bond for us: alert the guardian, "
                          "or run a disputer watcher")
        bond = d.cfg.dispute_bond
        need = bond + self.disputer_fee
        coins = sorted((c for c in self.chain.unspent_at([self.disputer_ph]) if c.amount >= need), key=lambda c: c.amount)
        if not coins:
            return Remedy("dispute", h, refused=f"the disputer address holds no coin of at least {need:,} mojos "
                          f"(the bond is {bond:,}); fund it")
        coin = coins[0]
        remedy = self._prepare("dispute", h, lambda: [d.dispute(h, report.root_id, self.disputer_ph, coin.coin_id())],
                               note=f"the disputer's coin spend must create the bond launcher and confirm before "
                                    f"height {report.activates_at}; a rejected dispute forfeits the bond")
        remedy.disputer_coin = coin
        return remedy

    def _prepare(self, action: str, h: int, calls, upheld: Optional[int] = None, note: Optional[str] = None) -> Remedy:
        try:
            prepared = self._got.distributor.prepare(calls())
        except Exception as e:  # noqa: BLE001
            return Remedy(action, h, upheld=upheld, refused=f"the leaf refuses it ({type(e).__name__}: {e})")
        return Remedy(action, h, prepared, upheld=upheld, note=note)


# --- alerts -------------------------------------------------------------------------------


def post_webhook(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "flpcr-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


class AlertSink:
    """Prints and posts each distinct alert once."""

    def __init__(self, webhook: Optional[str] = None, out=sys.stdout):
        self.webhook = webhook
        self.out = out
        self.sent: set = set()

    def __call__(self, report: Report) -> bool:
        if not report.should_alert or report.key() in self.sent:
            return False
        self.sent.add(report.key())
        payload = report.alert()
        print(f"flpcr-watch: {payload['text']}", file=self.out, flush=True)
        if self.webhook:
            try:
                post_webhook(self.webhook, payload)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self.sent.discard(report.key())  # retried next pass
        return True


# --- CLI ----------------------------------------------------------------------------------


EXIT = {MATCH: 0, NO_PENDING: 0, MISMATCH: 1, UNVERIFIABLE: 3}


def main(argv: Optional[List[str]] = None) -> int:
    from chia_wallet_sdk import Address

    from .chain import NodeChain
    from .main import NODES, ROUTERS, live_source_factory

    env = os.environ
    p = argparse.ArgumentParser(prog="python -m flpcr.service.watcher", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", help="a deploy_testnet.py state file (reads launcher_id and network)")
    p.add_argument("--launcher", default=env.get("FLPCR_LAUNCHER", ""), help="launcher id, hex")
    p.add_argument("--network", default=env.get("FLPCR_NETWORK", ""), choices=["", *NODES])
    p.add_argument("--node", default=env.get("FLPCR_NODE_URL", ""), help="full node or coinset RPC")
    p.add_argument("--api", default=env.get("FLPCR_FORGE_API", ""), help="Forge router (pool -> LP asset)")
    p.add_argument("--cache-dir", default=env.get("FLPCR_CACHE_DIR", "data"))
    p.add_argument("--role", default="observer", choices=ROLES)
    p.add_argument("--disputer", help="the disputer's address (xch1/txch1 or hex); role disputer")
    p.add_argument("--manifests", default=env.get("FLPCR_MANIFESTS", ""),
                   help="folder of <hash>.json, or URL template with {hash}/{root_id}")
    p.add_argument("--require-manifest", action="store_true", help="a root without a committed manifest is UNVERIFIABLE")
    p.add_argument("--webhook", default=env.get("FLPCR_WATCH_WEBHOOK", ""), help="POST each alert's JSON here")
    p.add_argument("--once", action="store_true", help="one check; exit 0 match/none, 1 mismatch, 3 unverifiable")
    p.add_argument("--every", type=int, default=30, help="seconds between checks (default 30)")
    p.add_argument("--json", action="store_true", help="print each report as JSON (remedy spends included)")
    args = p.parse_args(argv)

    launcher, network = args.launcher, args.network
    if args.state:
        rec = json.loads(pathlib.Path(args.state).read_text())
        launcher = launcher or rec.get("launcher_id", "")
        network = network or rec.get("network", "")
    launcher = launcher.strip().removeprefix("0x")
    if network not in NODES or len(launcher) != 64:
        p.error("give --state, or --launcher (64 hex) and --network (testnet11|mainnet)")
    node = args.node or NODES[network]
    api = args.api or ROUTERS[network]
    if args.role == "disputer" and not args.disputer:
        p.error("--role disputer needs --disputer (the address whose coin funds the bond and gets the refund)")
    disputer = None
    if args.disputer:
        text = args.disputer.strip()
        disputer = Address.decode(text).puzzle_hash if text.startswith(("xch1", "txch1")) else bytes.fromhex(text)

    watcher = Watcher(NodeChain(node), bytes.fromhex(launcher),
                      live_source_factory(network, node, api, pathlib.Path(args.cache_dir)), network=network,
                      role=args.role, disputer_ph=disputer, manifests=manifest_lookup(args.manifests),
                      require_manifest=args.require_manifest)
    sink = AlertSink(args.webhook or None)
    while True:
        try:
            report = watcher.check()
            sink(report)
            if args.json:
                print(json.dumps(report.to_json(), indent=2), flush=True)
            else:
                print(json.dumps(report.alert(), indent=2) if report.status != NO_PENDING or report.should_alert
                      else f"peak {report.peak}: no root pending", flush=True)
            if args.once:
                return EXIT[report.status]
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            if args.once:
                return 2
        time.sleep(args.every)


if __name__ == "__main__":
    raise SystemExit(main())
