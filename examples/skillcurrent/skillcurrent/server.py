"""HTTP server: one JSON-RPC style endpoint, the web UI and the beta page.

    POST /api/rpc        {"op": "<operation>", "args": {...}}
    POST /api/beta       the landing page's sign-up form (JSON or form-encoded)
    GET  /api/health     liveness (touches the database), version, auth mode
    GET  /               the web UI (the beta page in --public mode)
    GET  /beta           the beta landing page
    GET  /og.png, /fonts/<file>.woff2   the page's own assets

Authentication is a member token (``Authorization: Bearer ts_...``), issued
when a member is added. With ``--no-auth`` (demos on loopback only) the caller
names itself with ``X-SkillCurrent-Team`` and ``X-SkillCurrent-User``.

``--public`` is the mode for a host on the open internet whose only job is the
beta page and its waitlist: the web UI is not served and the RPC endpoint
answers only the waitlist operations, for the waitlist team's owners.

The server speaks plain HTTP. Put TLS in front of it (Caddy, a platform's
edge, a tunnel) before exposing it beyond a trusted network.
"""

import html
import json
import logging
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .errors import Forbidden, Invalid, NotFound, RateLimited, SkillCurrentError, Unauthorized
from .service import RPC_OPS, Service

log = logging.getLogger("skillcurrent.server")

WEB_DIR = Path(__file__).parent / "web"
ASSETS_DIR = WEB_DIR / "assets"
LANDING = WEB_DIR / "beta.html"
MAX_BODY = 4 * 1024 * 1024
MAX_BETA_BODY = 16 * 1024
# Operations the RPC endpoint still answers in --public mode.
PUBLIC_RPC_OPS = ("whoami", "list_beta_signups", "remove_beta_signup", "import_beta_signups")
HONEYPOT_FIELD = "website"
LOOPBACK = ("127.0.0.1", "localhost", "::1")

CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)


class RateLimiter:
    """At most ``limit`` events per ``window`` seconds per key (a sliding window, in memory).

    Keys are client addresses. A key is forgotten within ``window`` plus one
    sweep interval of its last event, so the page can truthfully say an
    address is held for about ten minutes and never written down.
    """

    def __init__(self, limit: int, window: float, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.sweep_every = min(60.0, window)
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._last_sweep = clock()

    def _sweep(self, now: float) -> None:
        for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
            del self._hits[k]
        self._last_sweep = now

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            if now - self._last_sweep >= self.sweep_every or len(self._hits) > 10000:
                self._sweep(now)
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def __len__(self) -> int:
        return len(self._hits)


ENDPOINT_MODES = ("netlify", "mailto")


def valid_endpoint(endpoint: str) -> bool:
    """'/path' on the same host, an http(s) URL, or one of ENDPOINT_MODES."""
    return endpoint in ENDPOINT_MODES or endpoint.startswith("/") or endpoint.startswith(("https://", "http://"))


KEEP = "We keep what you type here, plus which link brought you, only to reply and to choose beta teams."
NO_TRACKING = "No cookies, no analytics, no third-party requests."


def privacy_note(endpoint: str, logs_addresses: bool = False) -> str:
    """The privacy sentence under the form, true for how this page submits.

    ``logs_addresses`` is whether the skillcurrent server receiving the form
    writes a request log (``serve --access-log``, or a team server's default).
    """
    if endpoint == "mailto":
        return ("Nothing is sent until you press send in your email app. The email holds your entry and which link brought you; "
                f"we keep it only to reply and to choose beta teams. {NO_TRACKING}")
    if endpoint == "netlify":
        return (f"{KEEP} Netlify hosts this page and stores the form for us, and may record your network address with it. {NO_TRACKING}")
    if logs_addresses:
        address = "This server's request log records your network address."
    else:
        address = "Your network address is used only in memory, for about ten minutes, to limit spam; it is never written to disk."
    if endpoint.startswith(("https://", "http://")):
        address += " The host serving this page may keep its own standard logs."
    return f"{KEEP} {address} {NO_TRACKING}"


def render_landing(template: str, *, endpoint: str = "/api/beta", site_url: str = "", contact: str = "", logs_addresses: bool = False) -> str:
    """Fill the beta page's placeholders. Used by ``serve`` and by ``build-landing``.

    ``endpoint`` picks how the form submits: a same-host path or URL of a
    skillcurrent server (JSON, and a plain form POST without JavaScript),
    ``netlify`` (Netlify Forms on a static deploy), or ``mailto`` (no server:
    the visitor's email app opens with the entry, addressed to ``contact``).
    """
    if not valid_endpoint(endpoint):
        raise ValueError(f"unsupported endpoint {endpoint!r}")
    esc = lambda v: html.escape(v, quote=True)
    extra = ""
    if endpoint == "netlify":
        attrs = 'method="post" action="/?joined=1" name="beta" data-netlify="true" netlify-honeypot="website"'
        extra = '\n        <input type="hidden" name="form-name" value="beta">'
    elif endpoint == "mailto":
        attrs = f'method="post" enctype="text/plain" action="mailto:{esc(contact)}"' if contact else 'method="post" action=""'
    else:
        attrs = f'method="post" action="{esc(endpoint)}"'
    out = template.replace("__FORM_ATTRS__", attrs).replace("__FORM_EXTRA__", extra)
    for key, value in (
        ("__ENDPOINT__", endpoint), ("__SITE_URL__", site_url.rstrip("/")), ("__CONTACT__", contact),
        ("__PRIVACY_NOTE__", privacy_note(endpoint, logs_addresses)),
    ):
        out = out.replace(key, esc(value))
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "SkillCurrent"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 30  # drop idle or slow clients instead of holding a thread forever

    service: Service
    no_auth: bool = False
    public: bool = False
    allow_origins: frozenset = frozenset()
    trust_proxy: bool = False
    contact: str = ""
    site_url: str = ""
    beta_limiter: RateLimiter | None = None
    access_log: bool = False

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):
        if self.access_log:
            log.info("%s %s", self._client_ip(), fmt % args)

    def log_error(self, fmt, *args):
        # Malformed requests and timeouts are worth seeing even without an access log; the address is left out.
        log.warning("%s", fmt % args)

    def _client_ip(self) -> str:
        if self.trust_proxy:
            forwarded = self.headers.get("X-Forwarded-For", "") if hasattr(self, "headers") and self.headers else ""
            if forwarded:
                return forwarded.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "?"

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def _cors_origin(self) -> str | None:
        origin = self.headers.get("Origin", "")
        return origin if origin and origin in self.allow_origins else None

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict, extra: dict | None = None) -> None:
        headers = {"Cache-Control": "no-store"}
        headers.update(extra or {})
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", headers)

    def _send_error(self, exc: SkillCurrentError, extra: dict | None = None) -> None:
        self._send_json(exc.http_status, {"ok": False, "error": {"code": exc.code, "message": exc.message}}, extra)

    def _not_found(self) -> None:
        self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "no such route"}})

    def _send_html(self, text: str) -> None:
        self._send(200, text.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": CSP, "Cache-Control": "no-cache"})

    def _read_body(self, limit: int) -> bytes:
        raw = self.headers.get("Content-Length")
        if raw is None or raw.strip() == "":
            return b""
        try:
            length = int(raw)
        except ValueError:
            raise Invalid("Content-Length must be a whole number") from None
        if length < 0:
            raise Invalid("Content-Length must not be negative")
        if length > limit:
            raise Invalid("request body too large")
        return self.rfile.read(length) if length else b""

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

    def _site_url(self) -> str:
        if self.site_url:
            return self.site_url
        host = self.headers.get("Host", "")
        proto = self.headers.get("X-Forwarded-Proto", "") if self.trust_proxy else ""
        return f"{proto or 'http'}://{host}" if host else ""

    def _landing(self) -> None:
        if not LANDING.exists():
            raise NotFound("the beta page is not present in this install")
        page = render_landing(
            LANDING.read_text(encoding="utf-8"), endpoint="/api/beta", site_url=self._site_url(), contact=self.contact, logs_addresses=self.access_log
        )
        self._send_html(page)

    def _asset(self, path: str) -> None:
        name = path.rsplit("/", 1)[-1]
        if path == "/og.png":
            file, ctype = ASSETS_DIR / "og.png", "image/png"
        elif path.startswith("/fonts/") and name.endswith(".woff2") and "/" not in name and not name.startswith("."):
            file, ctype = ASSETS_DIR / "fonts" / name, "font/woff2"
        else:
            raise NotFound("no such route")
        if not file.is_file():
            raise NotFound("no such asset")
        self._send(200, file.read_bytes(), ctype, {"Cache-Control": "public, max-age=86400"})

    # -- routes ------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/health":
                try:
                    self.service.store.one("SELECT 1 AS ok")
                except Exception:
                    log.exception("health check could not read the database")
                    self._send_json(503, {"ok": False, "error": {"code": "unavailable", "message": "database unavailable"}})
                    return
                body = {"ok": True, "version": __version__, "auth": "none" if self.no_auth else "token", "public": self.public}
                if not self.public:
                    body["ops"] = RPC_OPS
                self._send_json(200, body)
            elif path in ("/beta", "/beta/", "/landing", "/landing/") or (self.public and path in ("/", "/index.html")):
                self._landing()
            elif path in ("/", "/index.html", "/app", "/app/"):
                if self.public:
                    raise NotFound("no such route")
                self._send_html((WEB_DIR / "index.html").read_text(encoding="utf-8"))
            elif path.endswith("/og.png"):
                self._asset("/og.png")
            elif "/fonts/" in path:
                self._asset("/fonts/" + path.rsplit("/", 1)[-1])
            else:
                self._not_found()
        except SkillCurrentError as exc:
            self._send_error(exc)
        except Exception:
            log.exception("GET %s failed", path)
            self._send_json(500, {"ok": False, "error": {"code": "error", "message": "internal error"}})

    def do_OPTIONS(self):
        path = self.path.split("?", 1)[0]
        origin = self._cors_origin()
        if path == "/api/beta" and origin:
            self._send(204, b"", "text/plain", {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "POST",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
            })
        else:
            self._send(204, b"", "text/plain")

    def _beta_signup(self) -> None:
        origin = self._cors_origin()
        cors = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if origin else {}
        ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        form_post = ctype == "application/x-www-form-urlencoded"
        try:
            if self.beta_limiter is not None and not self.beta_limiter.allow(self._client_ip()):
                raise RateLimited("too many sign-ups from this address; try again in a few minutes")
            raw = self._read_body(MAX_BETA_BODY)
            if form_post:
                fields = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
                payload = {k: (v if k == "tools" else v[0]) for k, v in fields.items()}
            else:
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    raise Invalid("request body must be JSON") from None
                if not isinstance(payload, dict):
                    raise Invalid("request body must be a JSON object")
            if str(payload.get(HONEYPOT_FIELD) or "").strip():
                # A field humans never see was filled in: answer as if it worked, store nothing.
                log.info("beta sign-up dropped by honeypot")  # no address: the page promises we keep none
                result = {"email": "", "team_size": "", "tools": [], "new": False}
            else:
                result = self.service.record_beta_signup(
                    payload.get("email", ""), payload.get("team_size", ""), payload.get("tools"), payload.get("note", ""), payload.get("source", "")
                )
                log.info("beta sign-up %s", "new" if result["new"] else "repeat")
            if form_post:
                # A browser without JavaScript: send it back to the page with a thank-you.
                self._send(303, b"", "text/plain", {"Location": "/beta?joined=1", **cors})
            else:
                self._send_json(200, {"ok": True, "result": {"new": result["new"]}}, cors)
        except SkillCurrentError as exc:
            self._send_error(exc, cors)
        except Exception:
            log.exception("beta sign-up failed")
            self._send_json(500, {"ok": False, "error": {"code": "error", "message": "internal error"}}, cors)

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route == "/api/beta":
            self._beta_signup()
            return
        if route != "/api/rpc":
            self._not_found()
            return
        op = None
        try:
            raw = self._read_body(MAX_BODY)
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                raise Invalid("request body must be JSON") from None
            if not isinstance(payload, dict):
                raise Invalid("request body must be a JSON object")
            op = payload.get("op")
            args = payload.get("args") or {}
            allowed = PUBLIC_RPC_OPS if self.public else RPC_OPS
            if op not in allowed:
                raise Invalid(f"unknown operation {op!r}" + (" (this server runs in --public mode)" if self.public and op in RPC_OPS else ""))
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
            self._send_error(exc)
        except Exception:  # never leak a traceback to clients, but always log it
            log.exception("rpc %s failed", op)
            self._send_json(500, {"ok": False, "error": {"code": "error", "message": "internal error"}})


def make_server(
    service: Service,
    host: str = "127.0.0.1",
    port: int = 8765,
    no_auth: bool = False,
    verbose: bool = False,
    *,
    public: bool = False,
    allow_origins=(),
    trust_proxy: bool = False,
    contact: str = "",
    site_url: str = "",
    beta_rate: tuple[int, float] | None = (5, 600.0),
    access_log: bool | None = None,
) -> ThreadingHTTPServer:
    """Build the server. ``beta_rate`` is (sign-ups, seconds) per client address; None disables it."""
    service.auth_mode = "none" if no_auth else "token"
    attrs = {
        "service": service,
        "no_auth": no_auth,
        "public": public,
        "allow_origins": frozenset(o.rstrip("/") for o in allow_origins if o),
        "trust_proxy": trust_proxy,
        "contact": contact or "",
        "site_url": (site_url or "").rstrip("/"),
        "beta_limiter": RateLimiter(*beta_rate) if beta_rate else None,
        "access_log": verbose if access_log is None else access_log,
    }
    handler = type("BoundHandler", (Handler,), attrs)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.verbose = verbose
    return server
