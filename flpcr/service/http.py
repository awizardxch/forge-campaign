"""The service's HTTP API (JSON, CORS-open for the Forge front end).

    GET  /health                         liveness: 200 once booted, with the peak
    GET  /api/v1/status                  the distributor: campaigns, roots, dispute, rotation
    GET  /api/v1/claimable/<address>     a holder's leaves in the claimable root, with the
                                         message to sign and the proof (xch1/txch1 or hex)
    POST /api/v1/claims                  the sequencer (flpcr.service.sequencer): signed claims
    GET  /api/v1/claims/<request id>     a submitted claim's progress
"""

from __future__ import annotations

import json
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


def make_handler(service, sequencer=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "flpcr-service/1.0"

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
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            try:
                if path == "/health":
                    if service.s is None:
                        return self.send_json(503, {"ok": False, "status": "booting"})
                    return self.send_json(200, {"ok": True, "peak": service.chain.peak(), "proof_gap": service.s.mismatch})
                if service.s is None:
                    return self.send_json(503, {"success": False, "error": "booting: resyncing from chain"})
                if path == "/api/v1/status":
                    return self.send_json(200, {"success": True, **service.status()})
                if path.startswith("/api/v1/claimable/"):
                    ph = puzzle_hash_of(path.rsplit("/", 1)[1])
                    return self.send_json(200, {"success": True, "puzzle_hash": ph.hex(), **service.claimable(ph)})
                if path.startswith("/api/v1/claims/") and sequencer is not None:
                    found = sequencer.status_of(path.rsplit("/", 1)[1])
                    return self.send_json(200 if found else 404, {"success": bool(found), **(found or {"error": "unknown request"})})
                return self.send_json(404, {"success": False, "error": "not found"})
            except ValueError as e:
                return self.send_json(400, {"success": False, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self.send_json(500, {"success": False, "error": f"{type(e).__name__}"})

        def do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 1_000_000:
                    return self.send_json(413, {"success": False, "error": "request too large"})
                body = json.loads(self.rfile.read(length) or b"{}")
                if path == "/api/v1/claims" and sequencer is not None:
                    return self.send_json(202, {"success": True, **sequencer.submit(body)})
                return self.send_json(404, {"success": False, "error": "not found"})
            except ValueError as e:
                return self.send_json(400, {"success": False, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self.send_json(500, {"success": False, "error": f"{type(e).__name__}"})

    return Handler


def serve(service, port: int, sequencer=None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(service, sequencer))
