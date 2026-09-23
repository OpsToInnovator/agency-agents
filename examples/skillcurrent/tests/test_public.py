"""Going-live behaviour: hardening, public mode, CORS, the waitlist, and the
static build of the beta page."""

import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from skillcurrent.cli import main
from skillcurrent.errors import Forbidden, Invalid
from skillcurrent.server import CSP, Handler, RateLimiter, make_server, privacy_note, render_landing
from skillcurrent.service import Service
from skillcurrent.store import Store


def start(service, **kw):
    kw.setdefault("beta_rate", None)
    srv = make_server(service, "127.0.0.1", 0, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def request(url, method="GET", body=None, headers=None):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def raw_http(port, text: bytes, timeout=5.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(text)
        chunks = []
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


@pytest.fixture
def served(service, team):
    srv, url = start(service)
    yield srv, url, team["tokens"]
    srv.shutdown()
    srv.server_close()


JSON = {"Content-Type": "application/json"}


# -------------------------------------------------------------- hardening
def test_bad_content_length_is_rejected_not_hung(served):
    srv, _, _ = served
    port = srv.server_address[1]
    for route in ("/api/beta", "/api/rpc"):
        for value in ("-1", "abc"):
            reply = raw_http(port, f"POST {route} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: {value}\r\n\r\n".encode())
            assert reply.startswith(b"HTTP/1.0 400"), (route, value, reply[:80])
            assert b'"code": "invalid"' in reply
    assert Handler.timeout == 30


def test_health_touches_db_and_supports_head(served, service):
    _, url, _ = served
    status, headers, body = request(url + "/api/health", "HEAD")
    assert status == 200 and body == b""
    status, _, body = request(url + "/api/health")
    assert status == 200 and json.loads(body)["public"] is False
    service.store.close()  # a broken database must not report healthy
    status, _, body = request(url + "/api/health")
    assert status == 503 and json.loads(body)["error"]["code"] == "unavailable"


def test_security_headers_and_no_python_version(served):
    _, url, _ = served
    status, headers, _ = request(url + "/beta")
    assert status == 200
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["X-Frame-Options"] == "DENY"
    assert headers["Content-Security-Policy"] == CSP
    assert headers["Server"].startswith("SkillCurrent") and "Python" not in headers["Server"]


def test_beta_endpoint_catch_all_returns_json(served, service, monkeypatch):
    _, url, _ = served
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(service, "record_beta_signup", boom)
    status, _, body = request(url + "/api/beta", "POST", {"email": "a@b.co"}, JSON)
    assert status == 500 and json.loads(body)["error"] == {"code": "error", "message": "internal error"}


def test_rate_limit_per_client(service, team):
    srv, url = start(service, beta_rate=(2, 600.0))
    try:
        codes = [request(url + "/api/beta", "POST", {"email": f"u{i}@x.co"}, JSON)[0] for i in range(3)]
        assert codes == [200, 200, 429]
        status, _, body = request(url + "/api/beta", "POST", {"email": "u9@x.co"}, JSON)
        assert json.loads(body)["error"]["code"] == "rate_limited"
    finally:
        srv.shutdown(); srv.server_close()


def test_rate_limiter_window():
    t = [0.0]
    rl = RateLimiter(2, 10, clock=lambda: t[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a") and rl.allow("b")
    t[0] = 11
    assert rl.allow("a")


def test_rate_limiter_forgets_addresses():
    """The page says an address is held for about ten minutes: quiet keys must be dropped, not kept until a restart."""
    t = [0.0]
    rl = RateLimiter(5, 600, clock=lambda: t[0])
    rl.allow("203.0.113.7")
    t[0] = 300
    rl.allow("198.51.100.1")
    assert len(rl) == 2
    t[0] = 661  # the first address is past its window plus one sweep interval
    rl.allow("192.0.2.9")
    assert len(rl) == 2  # 203.0.113.7 is gone; the other two are still inside their window
    t[0] = 2000
    rl.allow("192.0.2.9")
    assert len(rl) == 1


def test_signup_logs_carry_no_address(service, team, caplog):
    srv, url = start(service, trust_proxy=True, access_log=False)
    try:
        with caplog.at_level("INFO", logger="skillcurrent.server"):
            request(url + "/api/beta", "POST", {"email": "log@x.co"}, {**JSON, "X-Forwarded-For": "203.0.113.50"})
            request(url + "/api/beta", "POST", {"email": "bot@x.co", "website": "spam"}, {**JSON, "X-Forwarded-For": "203.0.113.51"})
            request(url + "/beta", headers={"X-Forwarded-For": "203.0.113.52"})
        text = caplog.text
        assert "beta sign-up new" in text and "honeypot" in text
        assert "203.0.113." not in text and "127.0.0.1" not in text
    finally:
        srv.shutdown(); srv.server_close()


def test_access_log_default_follows_mode(tmp_path, monkeypatch, capsys):
    """A team server logs requests by default; the public waitlist host does not unless asked."""
    from skillcurrent import server as server_mod

    seen = {}

    class Stop(Exception):
        pass

    def fake_make_server(service, host, port, **kw):
        seen["access_log"] = kw["access_log"]
        raise Stop

    monkeypatch.setattr(server_mod, "make_server", fake_make_server)
    db = str(tmp_path / "db.sqlite")
    for argv, want in (
        ([], True),
        (["--public"], False),
        (["--public", "--access-log"], True),
        (["--access-log", "--quiet"], False),
        (["--quiet"], False),
    ):
        with pytest.raises(Stop):
            main(["--db", db, "serve", *argv])
        assert seen["access_log"] is want, argv


def test_privacy_note_matches_how_the_form_submits():
    served = privacy_note("/api/beta")
    assert "never written to disk" in served and "about ten minutes" in served and "No cookies" in served
    assert "request log records your network address" in privacy_note("/api/beta", logs_addresses=True)
    assert "never written to disk" not in privacy_note("/api/beta", logs_addresses=True)
    assert "host serving this page may keep its own standard logs" in privacy_note("https://api.example.com/api/beta")
    assert "Netlify" in privacy_note("netlify") and "never written" not in privacy_note("netlify")
    mail = privacy_note("mailto")
    assert "until you press send" in mail and "never written" not in mail


def test_served_page_privacy_note_tracks_the_access_log(service, team):
    for access_log, phrase in ((False, "never written to disk"), (True, "request log records your network address")):
        srv, url = start(service, public=True, access_log=access_log)
        try:
            page = request(url + "/")[2].decode()
            assert phrase in page and "__PRIVACY_NOTE__" not in page
        finally:
            srv.shutdown(); srv.server_close()


def test_honeypot_stores_nothing(served, service):
    _, url, _ = served
    status, _, body = request(url + "/api/beta", "POST", {"email": "bot@spam.co", "website": "http://spam"}, JSON)
    assert status == 200 and json.loads(body)["ok"] is True
    assert service.list_beta_signups("acme", "ana") == []


def test_form_post_without_javascript_redirects(served, service):
    _, url, _ = served
    body = urllib.parse.urlencode([("email", "nojs@example.com"), ("team_size", "5-15"), ("tools", "codex"), ("tools", "claude-code"), ("website", "")]).encode()
    req = urllib.request.Request(url + "/api/beta", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as exc:
        opener.open(req, timeout=10)
    assert exc.value.code == 303 and exc.value.headers["Location"] == "/beta?joined=1"
    row = service.list_beta_signups("acme", "ana")[0]
    assert row["email"] == "nojs@example.com" and row["tools"] == ["claude-code", "codex"]


def test_signup_values_limited_to_form_choices(service, team):
    r = service.record_beta_signup("x@y.co", "a lot", ["codex", "evil<script>", "CLAUDE-CODE"], "", "")
    assert r["team_size"] == "" and r["tools"] == ["claude-code", "codex"]


# ------------------------------------------------------------------- CORS
def test_cors_only_for_allowed_origins(service, team):
    srv, url = start(service, allow_origins=["https://beta.example.com"])
    try:
        status, headers, _ = request(url + "/api/beta", "OPTIONS", None, {"Origin": "https://beta.example.com", "Access-Control-Request-Method": "POST"})
        assert status == 204 and headers["Access-Control-Allow-Origin"] == "https://beta.example.com"
        assert "POST" in headers["Access-Control-Allow-Methods"]
        status, headers, _ = request(url + "/api/beta", "POST", {"email": "c@d.co"}, {**JSON, "Origin": "https://beta.example.com"})
        assert status == 200 and headers["Access-Control-Allow-Origin"] == "https://beta.example.com"
        status, headers, _ = request(url + "/api/beta", "OPTIONS", None, {"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in headers
        status, headers, _ = request(url + "/api/beta", "POST", {"email": "e@f.co"}, {**JSON, "Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in headers  # the browser will refuse to hand the reply to that page
    finally:
        srv.shutdown(); srv.server_close()


# ------------------------------------------------------------- public mode
def test_public_mode_serves_only_the_waitlist(service, team):
    srv, url = start(service, public=True, contact="beta@example.com", site_url="https://beta.example.com")
    tok = team["tokens"]
    try:
        status, _, body = request(url + "/")
        assert status == 200 and b"Better methods." in body
        assert b'content="https://beta.example.com/og.png"' in body and b'data-contact="beta@example.com"' in body
        for path in ("/app", "/index.html?x=1"):
            status, _, body = request(url + path)
            assert path == "/index.html?x=1" or status == 404
        status, _, body = request(url + "/api/health")
        assert json.loads(body)["public"] is True and "ops" not in json.loads(body)
        status, headers, body = request(url + "/og.png")
        assert status == 200 and headers["Content-Type"] == "image/png" and body[:4] == b"\x89PNG"
        status, headers, _ = request(url + "/fonts/fraunces.woff2")
        assert status == 200 and headers["Content-Type"] == "font/woff2"
        assert request(url + "/beta/fonts/ibm-plex-sans.woff2")[0] == 200
        assert request(url + "/fonts/../server.py")[0] == 404
        # RPC answers only waitlist operations, still behind tokens and the waitlist-team rule.
        auth = {**JSON, "Authorization": f"Bearer {tok['ana']}"}
        status, _, body = request(url + "/api/rpc", "POST", {"op": "list_skills"}, auth)
        assert status == 400 and "--public" in json.loads(body)["error"]["message"]
        assert request(url + "/api/rpc", "POST", {"op": "list_beta_signups"}, auth)[0] == 200
        assert request(url + "/api/rpc", "POST", {"op": "list_beta_signups"}, JSON)[0] == 401
    finally:
        srv.shutdown(); srv.server_close()


def test_waitlist_scoped_to_one_team(service, team):
    service.record_beta_signup("lead@example.com", "5-15", ["codex"], "", "")
    other = service.create_team("other", "Other", "zed")
    # With two teams and no configured waitlist team, nobody may read it.
    with pytest.raises(Forbidden, match="SKILLCURRENT_WAITLIST_TEAM"):
        service.list_beta_signups("acme", "ana")
    scoped = Service(service.store, waitlist_team="acme")
    assert scoped.list_beta_signups("acme", "ana")[0]["email"] == "lead@example.com"
    with pytest.raises(Forbidden, match="belongs to team 'acme'"):
        scoped.list_beta_signups("other", "zed")  # an owner of another team is refused
    assert other["token"]
    assert scoped.remove_beta_signup("acme", "ana", "LEAD@example.com") == {"email": "lead@example.com", "removed": True}
    assert scoped.remove_beta_signup("acme", "ana", "lead@example.com")["removed"] is False


def test_import_merges_and_skips(service, team):
    service.record_beta_signup("a@x.co", "", ["codex"], "first", "")
    r = service.import_beta_signups("acme", "ana", [
        {"email": "a@x.co", "team_size": "5-15", "tools": "claude-code", "note": "second"},
        {"email": "b@x.co", "team_size": "1-4", "tools": ["osaurus"], "created_at": "2026-09-01T10:00:00Z"},
        {"email": "not an email"},
        "junk",
    ])
    assert r == {"added": 1, "merged": 1, "skipped": 2}
    rows = {x["email"]: x for x in service.list_beta_signups("acme", "ana")}
    assert rows["a@x.co"]["team_size"] == "5-15" and rows["a@x.co"]["tools"] == ["codex", "claude-code"]
    assert rows["b@x.co"]["created_at"] == "2026-09-01T10:00:00Z"
    with pytest.raises(Invalid):
        service.import_beta_signups("acme", "ana", "nope")


# ------------------------------------------------------------ landing page
TEMPLATE = (Path(__file__).resolve().parent.parent / "skillcurrent" / "web" / "beta.html").read_text(encoding="utf-8")


def test_render_modes():
    same = render_landing(TEMPLATE, endpoint="/api/beta", site_url="https://b.example.com/", contact='x"@y.co')
    assert 'action="/api/beta"' in same and 'data-endpoint="/api/beta"' in same
    assert 'data-contact="x&quot;@y.co"' in same  # contact is escaped
    for placeholder in ("__ENDPOINT__", "__SITE_URL__", "__CONTACT__", "__FORM_ATTRS__", "__FORM_EXTRA__", "__PRIVACY_NOTE__"):
        assert placeholder not in same
    netlify = render_landing(TEMPLATE, endpoint="netlify", site_url="https://b.example.com")
    assert 'data-netlify="true"' in netlify and 'netlify-honeypot="website"' in netlify and 'name="form-name" value="beta"' in netlify
    mail = render_landing(TEMPLATE, endpoint="mailto", site_url="https://b.example.com", contact="beta@y.co")
    assert 'action="mailto:beta@y.co"' in mail
    with pytest.raises(ValueError):
        render_landing(TEMPLATE, endpoint="javascript:alert(1)")


def test_page_has_no_third_party_requests():
    for needle in ("fonts.googleapis", "fonts.gstatic", "window.claude", "<script src"):
        assert needle not in TEMPLATE


def test_page_stores_nothing_on_the_device():
    """The privacy line says no cookies; the page keeps no other browser storage either."""
    for needle in ("document.cookie", "localStorage", "sessionStorage", "indexedDB"):
        assert needle not in TEMPLATE


def test_page_label_is_accurate():
    """The source is public, so the page must not call the beta private."""
    assert "private beta" not in TEMPLATE.lower() and "Beta pilot" in TEMPLATE


def test_fonts_ship_with_their_licence():
    fonts = Path(__file__).resolve().parent.parent / "skillcurrent" / "web" / "assets" / "fonts"
    text = (fonts / "OFL.txt").read_text(encoding="utf-8")
    assert "The Fraunces Project Authors" in text and 'IBM Corp. with Reserved Font Name "Plex"' in text
    assert "SIL OPEN FONT LICENSE Version 1.1" in text


def test_build_landing_for_a_static_host(tmp_path, capsys):
    out = tmp_path / "site"
    code = main(["build-landing", str(out), "--site-url", "https://beta.example.com", "--endpoint", "netlify", "--contact", "beta@example.com"])
    assert code == 0
    files = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    assert files == [
        "_headers", "fonts/OFL.txt", "fonts/fraunces.woff2", "fonts/ibm-plex-mono-400.woff2", "fonts/ibm-plex-mono-500.woff2",
        "fonts/ibm-plex-sans.woff2", "index.html", "og.png",
    ]
    assert "SIL OPEN FONT LICENSE Version 1.1" in (out / "fonts" / "OFL.txt").read_text(encoding="utf-8")
    page = (out / "index.html").read_text()
    assert 'content="https://beta.example.com/og.png"' in page and 'data-netlify="true"' in page
    assert "Netlify hosts this page and stores the form" in page
    headers = (out / "_headers").read_text()
    assert "Content-Security-Policy" in headers and "connect-src 'self';" in headers
    main(["build-landing", str(tmp_path / "x"), "--site-url", "https://p.example.com", "--endpoint", "https://api.example.com/api/beta"])
    assert "connect-src 'self' https://api.example.com;" in (tmp_path / "x" / "_headers").read_text()
    capsys.readouterr()
    assert main(["build-landing", str(tmp_path / "y"), "--site-url", "beta.example.com"]) == 1
    assert main(["build-landing", str(tmp_path / "y"), "--site-url", "https://b.example.com", "--endpoint", "mailto"]) == 1
    assert "needs --contact" in capsys.readouterr().err


def test_serve_refuses_no_auth_on_a_public_interface(tmp_path, capsys):
    code = main(["--db", str(tmp_path / "db.sqlite"), "serve", "--host", "0.0.0.0", "--no-auth"])
    assert code == 1 and "--host 127.0.0.1" in capsys.readouterr().err


# ------------------------------------------------------------------ store
def test_schema_version_and_additive_migration(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE beta_signups (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, team_size TEXT NOT NULL DEFAULT '',"
        " tools TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        " INSERT INTO beta_signups (email, created_at, updated_at) VALUES ('kept@x.co', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z');"
    )
    conn.commit(); conn.close()
    store = Store(path)
    assert store.schema_version == 2
    rows = store.beta_signups()
    assert rows[0]["email"] == "kept@x.co" and rows[0]["submissions"] == 1  # existing rows survive the upgrade
    store.close()
    assert Store(path).schema_version == 2  # idempotent


def test_backup_is_a_consistent_copy(tmp_path, capsys):
    db = tmp_path / "live.sqlite"
    assert main(["--db", str(db), "init", "--team", "acme", "--owner", "ana"]) == 0
    assert main(["--db", str(db), "backup", str(tmp_path / "copies" / "b.sqlite")]) == 0
    copy = Store(tmp_path / "copies" / "b.sqlite")
    assert [t["slug"] for t in copy.teams()] == ["acme"]
    with pytest.raises(ValueError):
        Store(db).backup(db)
    capsys.readouterr()
