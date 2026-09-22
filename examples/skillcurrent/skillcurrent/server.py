"""HTTP server: one JSON-RPC style endpoint plus the single-page web UI.

    POST /api/rpc      {"op": "<operation>", "args": {...}}
    GET  /api/health   liveness, version and the auth mode
    GET  /             the web UI

Authentication is a member token (``Authorization: Bearer ts_...``), issued
when a member is added. With ``--no-auth`` (demos, trusted networks) the
caller names itself with ``X-SkillCurrent-Team`` and ``X-SkillCurrent-User``.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .errors import Forbidden, Invalid, SkillCurrentError, Unauthorized
from .service import RPC_OPS, Service

WEB_DIR = Path(__file__).parent / "web"
LANDING = Path(__file__).parent.parent / "landing" / "index.html"
MAX_BODY = 4 * 1024 * 1024
MAX_BETA_BODY = 16 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = f"SkillCurrent/{__version__}"
    service: Service
    no_auth: bool = False

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):  # quiet by default; the CLI prints the URL
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _identity(self) -> tuple[str, str]:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            found = self.service.authenticate(auth[7:].strip())
            if not found:
                raise Unauthorized("invalid or revoked token")
            return found
        if self.no_auth:
            team = self.headers.get("X-SkillCurrent-Team", "").strip()
            user = self.headers.get("X-SkillCurrent-User", "").strip()
            if team and user:
                return team, user
            raise Unauthorized("send X-SkillCurrent-Team and X-SkillCurrent-User headers (server runs with --no-auth)")
        raise Unauthorized("send an 'Authorization: Bearer <token>' header")

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            self._send_json(200, {"ok": True, "version": __version__, "auth": "none" if self.no_auth else "token", "ops": RPC_OPS})
        elif path in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        elif path in ("/beta", "/beta/", "/landing", "/landing/"):
            if LANDING.exists():
                self._send_file(LANDING, "text/html; charset=utf-8")
            else:
                self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "landing/index.html is not present in this install"}})
        else:
            self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "no such route"}})

    def _beta_signup(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BETA_BODY:
                raise Invalid("request body too large")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                raise Invalid("request body must be JSON") from None
            if not isinstance(payload, dict):
                raise Invalid("request body must be a JSON object")
            result = self.service.record_beta_signup(
                payload.get("email", ""), payload.get("team_size", ""), payload.get("tools"), payload.get("note", ""), payload.get("source", "")
            )
            self._send_json(200, {"ok": True, "result": result})
        except SkillCurrentError as exc:
            self._send_json(exc.http_status, {"ok": False, "error": {"code": exc.code, "message": exc.message}})

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route == "/api/beta":
            self._beta_signup()
            return
        if route != "/api/rpc":
            self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "no such route"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise Invalid("request body too large")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                raise Invalid("request body must be JSON") from None
            if not isinstance(payload, dict):
                raise Invalid("request body must be a JSON object")
            op = payload.get("op")
            args = payload.get("args") or {}
            if op not in RPC_OPS:
                raise Invalid(f"unknown operation {op!r}")
            if not isinstance(args, dict):
                raise Invalid("'args' must be an object")
            team, actor = self._identity()
            if "team" in args and args.pop("team") != team:
                raise Forbidden("token does not belong to that team")
            args.pop("actor", None)
            try:
                result = getattr(self.service, op)(team, actor, **args)
            except TypeError as exc:
                raise Invalid(f"bad arguments for {op}: {exc}") from None
            self._send_json(200, {"ok": True, "result": result})
        except SkillCurrentError as exc:
            self._send_json(exc.http_status, {"ok": False, "error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:  # pragma: no cover - defensive; never leak a traceback to clients
            self._send_json(500, {"ok": False, "error": {"code": "error", "message": f"internal error: {type(exc).__name__}"}})


def make_server(service: Service, host: str = "127.0.0.1", port: int = 8765, no_auth: bool = False, verbose: bool = False) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"service": service, "no_auth": no_auth})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.verbose = verbose
    return server
