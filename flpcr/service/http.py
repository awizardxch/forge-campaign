"""The service's HTTP API (JSON, CORS-open for the Forge front end).

    GET  /health                         liveness: 200 once booted, with the peak
    GET  /api/v1/status                  the distributor: campaigns, roots (each with the
                                         manifest_hash it committed, or null), dispute, rotation
    GET  /api/v1/distributors            the registry for this network (flpcr.registry): every listed
                                         distributor, which are current, and which one this is
    GET  /api/v1/router-rules            what the router signs for in this distributor's reward asset:
                                         its minimum amount (a CAT's: the equivalent of $1 at its Forge
                                         price, or the operator's minimum), whether the operator has the
                                         CAT on for campaigns, kinds and claim-window floor
    GET  /api/v1/manifests/<hash>        a posted root's manifest (flpcr.manifest.root_to_json),
                                         by the manifest_hash its post_root committed; ".json"
                                         may be appended. Served as published: check it against
                                         the chain (the watcher does). A watcher reads it with
                                         FLPCR_MANIFESTS=https://<host>/api/v1/manifests/{hash}
    GET  /api/v1/claimable/<address>     a holder's leaves in the claimable root, with the
                                         message to sign and the proof (xch1/txch1 or hex),
                                         what each has claimed, and "pending": amounts in a
                                         posted root that is not claimable yet
    POST /api/v1/claims                  the sequencer (flpcr.service.sequencer): signed claims
    GET  /api/v1/claims/<request id>     a submitted claim's progress
    GET  /api/v1/watch                   the watcher's last report on the pending root (FLPCR_WATCH=1)
    POST /api/v1/campaigns/quote         the create intake (flpcr.service.intake): a campaign's
                                         terms, the creator's conditions and a lease
    POST /api/v1/campaigns/create        the creator's signed bundle for a quote; the router co-signs
    GET  /api/v1/campaigns/<campaign id> one campaign's page: status row, terms, what the schedule
                                         has released, its total and holder count per root

A quote that can no longer be honoured (lapsed lease, moved singleton) answers 409 with
"requote": true; another create holding the distributor answers 503 with "retry_after".
A create on a distributor that takes no new campaigns answers 409 with "distributor": the
reward asset's canonical launcher id (flpcr.service.fleet).

With a fleet (flpcr.service.fleet, one service hosting a distributor per reward asset), every
path above is also served per distributor:

    /d/<launcher id>/api/v1/...          that hosted distributor
    /a/<asset id>/api/v1/...             the reward asset's canonical distributor
    GET  /api/v1/fleet                   every hosted distributor, which takes creates, the build pin
    GET  /api/v1/launch/terms/<asset>    the router's terms for an asset with no distributor yet
    POST /api/v1/launch/quote            a first campaign that also launches its asset's distributor
    POST /api/v1/launch/create           the creator's signed launch bundle; the router co-signs

and the bare `/api/v1/...` paths answer for the primary distributor, as before.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from chia_wallet_sdk import Address


def puzzle_hash_of(text: str) -> bytes:
    text = text.strip()
    if text.startswith(("xch1", "txch1")):
        return Address.decode(text).puzzle_hash
    raw = bytes.fromhex(text.removeprefix("0x"))
    if len(raw) != 32:
        raise ValueError("an address or a 32-byte puzzle hash")
    return raw


class PostLimit:
    """At most `per_minute` POSTs a minute from one client (audit 2026-11-10, CS-01, CS-09).
    Every POST costs the service real work (a resync check and a fee estimate for a quote,
    CLVM runs for a bundle), and none needs a key. The client is the last address in
    X-Forwarded-For, which the proxy in front (Caddy) sets to the real peer; without the
    header, the socket's peer."""

    def __init__(self, per_minute: int = 30, clock=time.time):
        self.per_minute, self.clock = per_minute, clock
        self.hits: dict = {}
        self.lock = threading.Lock()

    def allow(self, client: str) -> bool:
        now = self.clock()
        with self.lock:
            if len(self.hits) > 10_000:  # forget idle clients
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < 60}
            recent = [t for t in self.hits.get(client, []) if now - t < 60]
            if len(recent) >= self.per_minute:
                self.hits[client] = recent
                return False
            recent.append(now)
            self.hits[client] = recent
            return True


def _hex32(text: str) -> Optional[bytes]:
    try:
        raw = bytes.fromhex(text.removeprefix("0x"))
    except ValueError:
        return None
    return raw if len(raw) == 32 else None


def make_handler(service, sequencer=None, intake=None, watcher=None, post_limit: Optional[PostLimit] = None,
                 fleet=None, read_limit: Optional[PostLimit] = None):
    post_limit = post_limit or PostLimit()
    # A wallet's claimable read (POST /api/v1/claimable) is dictionary lookups (CS-03), and the 🪎
    # tab sends one per few hundred addresses to every distributor the host serves: it has its own,
    # larger allowance, so a big wallet does not spend the budget that guards quotes and bundles
    # (owner, 2026-11-27: "too many requests" on the rewards page).
    read_limit = read_limit or PostLimit(per_minute=240)
    fixed = (service, sequencer, intake, watcher)

    def resolve(path: str):
        """(service, sequencer, intake, watcher, the path inside it) for a request path; None for
        an unknown distributor. Without a fleet, the one distributor answers every path."""
        if fleet is None:
            return (*fixed, path)
        parts = path.split("/")
        if len(parts) >= 3 and parts[1] in ("d", "a"):
            key = _hex32(parts[2])
            h = None if key is None else fleet.get(key) if parts[1] == "d" else fleet.for_asset(key)
            if h is None:
                return None
            return (h.service, h.sequencer, h.intake, h.watcher, "/" + "/".join(parts[3:]))
        h = fleet.home()
        if h is None:
            if fleet.primary is None:  # template mode before the first launch: nothing to read yet
                return None
            return (*fixed, path)
        return (h.service, h.sequencer, h.intake, h.watcher if h.watcher is not None else watcher, path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "flpcr-service/1.0"
        timeout = 60  # a socket that sends nothing does not hold a thread forever (CS-09)

        def log_message(self, *a):  # quiet; the service logs what matters
            pass

        def send_json(self, code: int, body) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            # A public site reading a service on the user's own machine (the testnet 🪎 tab): Chrome's
            # private-network preflight asks for this before it sends the POST.
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()

        def do_GET(self):
            full = self.path.split("?", 1)[0].rstrip("/")
            try:
                if fleet is not None and full == "/api/v1/fleet":
                    return self.send_json(200, {"success": True, **fleet.describe()})
                if fleet is not None and full.startswith("/api/v1/launch/terms/"):
                    asset = _hex32(full.rsplit("/", 1)[1])
                    if asset is None:
                        raise ValueError("an asset id is 32 bytes of hex")
                    if not fleet.booted:
                        return self.send_json(503, {"success": False, "error": "booting: resyncing from chain"})
                    best = fleet.canonical(asset)
                    return self.send_json(200, {"success": True, **fleet.terms(asset),
                                                "distributor": best.hex() if best else None})
                if fleet is not None and full == "/health" and fleet.home() is None and fleet.primary is None:
                    # Template mode before the first launch: up, with no distributor yet.
                    if not fleet.booted:
                        return self.send_json(503, {"ok": False, "status": "booting"})
                    return self.send_json(200, {"ok": True, "peak": fleet.chain.peak(), "distributors": 0})
                found = resolve(full)
                if found is None:
                    return self.send_json(404, {"success": False, "error": "this service hosts no such distributor"})
                service, sequencer, intake, watcher, path = found
                if path == "/health":
                    if service.s is None:
                        return self.send_json(503, {"ok": False, "status": "booting"})
                    return self.send_json(200, {"ok": True, "peak": service.chain.peak(), "proof_gap": service.s.mismatch})
                if service.s is None:
                    return self.send_json(503, {"success": False, "error": "booting: resyncing from chain"})
                if path == "/api/v1/status":
                    return self.send_json(200, {"success": True, **service.status()})
                if path == "/api/v1/distributors":
                    return self.send_json(200, {"success": True, **service.distributors()})
                if path == "/api/v1/router-rules" and intake is not None:
                    return self.send_json(200, {"success": True, **intake.terms()})
                if path.startswith("/api/v1/manifests/"):
                    found = service.manifest(path.rsplit("/", 1)[1])
                    return self.send_json(200 if found else 404, found if found else {"success": False, "error": "unknown manifest"})
                if path.startswith("/api/v1/claimable/"):
                    ph = puzzle_hash_of(path.rsplit("/", 1)[1])
                    return self.send_json(200, {"success": True, "puzzle_hash": ph.hex(), **service.claimable(ph)})
                if path == "/api/v1/watch" and watcher is not None:
                    last = watcher.last
                    return self.send_json(200 if last else 503, {"success": bool(last), **(last.alert() if last else {"error": "no check yet"})})
                if path.startswith("/api/v1/claims/") and sequencer is not None:
                    found = sequencer.status_of(path.rsplit("/", 1)[1])
                    return self.send_json(200 if found else 404, {"success": bool(found), **(found or {"error": "unknown request"})})
                if path.startswith("/api/v1/campaigns/"):
                    cid = path.rsplit("/", 1)[1]
                    raw = bytes.fromhex(cid.removeprefix("0x"))
                    if len(raw) != 32:
                        raise ValueError("a campaign id is 32 bytes of hex")
                    found = intake.campaign(cid) if intake is not None else service.campaign(raw)
                    if found is not None and "status" not in found:
                        found = {**found, "status": "created"}
                    return self.send_json(200 if found else 404, {"success": bool(found), **(found or {"error": "unknown campaign"})})
                return self.send_json(404, {"success": False, "error": "not found"})
            except ValueError as e:
                return self.send_json(400, {"success": False, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self.send_json(500, {"success": False, "error": f"{type(e).__name__}"})

        def do_POST(self):
            full = self.path.split("?", 1)[0].rstrip("/")
            try:
                forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
                limit = read_limit if full.endswith("/api/v1/claimable") else post_limit
                if not limit.allow(forwarded or self.client_address[0]):
                    return self.send_json(429, {"success": False, "error": "too many requests; retry in a minute",
                                                "retry_after": 60})
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0:
                    raise ValueError("Content-Length must not be negative")
                if length > 1_000_000:
                    return self.send_json(413, {"success": False, "error": "request too large"})
                body = json.loads(self.rfile.read(length) or b"{}")
                if fleet is not None and full == "/api/v1/launch/quote":
                    return self.send_json(200, {"success": True, **fleet.launches.quote(body)})
                if fleet is not None and full == "/api/v1/launch/create":
                    return self.send_json(200, {"success": True, **fleet.launches.create(body)})
                found = resolve(full)
                if found is None:
                    return self.send_json(404, {"success": False, "error": "this service hosts no such distributor"})
                service, sequencer, intake, watcher, path = found
                if path == "/api/v1/claims" and sequencer is not None:
                    queued = sequencer.submit(body)
                    if getattr(sequencer, "batch_on_submit", False):
                        sequencer.kick()  # the claimant paid the fee: push now, not on the next pass
                    return self.send_json(202, {"success": True, **queued})
                # The creator's reclaim (owner, 2026-11-27): a quote, then the creator's signed spend.
                if path in ("/api/v1/reclaim/quote", "/api/v1/reclaim"):
                    if service.s is None:
                        return self.send_json(503, {"success": False, "error": "booting: resyncing from chain"})
                    desk = service.reclaims
                    return self.send_json(200, {"success": True, **(desk.quote(body) if path.endswith("/quote") else desk.submit(body))})
                if path == "/api/v1/claimable":
                    if service.s is None:
                        return self.send_json(503, {"success": False, "error": "booting: resyncing from chain"})
                    raw = body.get("addresses") or body.get("puzzle_hashes") or []
                    if not isinstance(raw, list):
                        raise ValueError("addresses is a list")
                    return self.send_json(200, {"success": True, **service.claimable_many([puzzle_hash_of(str(x)) for x in raw])})
                # A 🔐 lock's actions as the distributor's guardian or admin (owner, 2026-11-25 veto;
                # 2026-11-26 resolve, rotate, cancel-rotation, reclaim). The Forge router calls these
                # over the compose network when a lock proposes and executes one; Caddy refuses
                # */api/v1/guardian/* from outside, since a hold makes the service's writers wait.
                action = path.removeprefix("/api/v1/guardian/")
                if path.startswith("/api/v1/guardian/") and action in service.LOCK_ACTIONS:
                    if service.s is None:
                        return self.send_json(503, {"success": False, "error": "booting: resyncing from chain"})

                    def whole(name, allowed=None):
                        v = body.get(name)
                        if not isinstance(v, int) or isinstance(v, bool) or v < 0 or (allowed and v not in allowed):
                            raise ValueError(f"{name} is a whole number" + (f" in {sorted(allowed)}" if allowed else ""))
                        return v

                    def h32(name):
                        raw = str(body.get(name) or "").strip().lower().removeprefix("0x")
                        if len(raw) != 64:
                            raise ValueError(f"{name} is 32 bytes of hex")
                        return bytes.fromhex(raw)

                    sender = puzzle_hash_of(str(body.get("sender") or body.get("guardian") or ""))
                    args = {}
                    if action == "veto":
                        args = {"root_id": whole("root_id")}
                    elif action == "resolve":
                        args = {"root_id": whole("root_id"), "upheld": whole("upheld", {0, 1})}
                    elif action in ("rotate", "cancel-rotation"):
                        args = {"engine_ph": puzzle_hash_of(str(body.get("engine_ph") or ""))}
                    else:
                        args = {"campaign_id": h32("campaign_id")}
                    return self.send_json(200, {"success": True,
                                                **service.lock_action(action, sender, bool(body.get("hold")), **args)})
                if path == "/api/v1/guardian/release":
                    service.release_guardian()
                    return self.send_json(200, {"success": True})
                if path == "/api/v1/campaigns/quote" and intake is not None:
                    return self.send_json(200, {"success": True, **intake.quote(body)})
                if path == "/api/v1/campaigns/create" and intake is not None:
                    return self.send_json(200, {"success": True, **intake.create(body)})
                return self.send_json(404, {"success": False, "error": "not found"})
            except ValueError as e:
                if type(e).__name__ in ("Requote", "Moved"):
                    moved = getattr(e, "distributor", None)
                    return self.send_json(409, {"success": False, "error": str(e), "requote": True,
                                                **({"distributor": moved.hex()} if moved is not None else {})})
                if type(e).__name__ == "LeaseBusy":
                    return self.send_json(503, {"success": False, "error": str(e), "retry_after": e.retry_after})
                return self.send_json(400, {"success": False, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self.send_json(500, {"success": False, "error": f"{type(e).__name__}"})

    return Handler


def serve(service, port: int, sequencer=None, intake=None, watcher=None,
          post_limit: Optional[PostLimit] = None, fleet=None, read_limit: Optional[PostLimit] = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port),
                               make_handler(service, sequencer, intake, watcher, post_limit, fleet, read_limit))
