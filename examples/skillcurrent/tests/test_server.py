import json
import threading
import urllib.request

import pytest

from skillcurrent.errors import Forbidden, Invalid, Unauthorized
from skillcurrent.server import make_server
from skillcurrent.session import RemoteSession


@pytest.fixture
def server(service, team):
    srv = make_server(service, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}", team["tokens"]
    srv.shutdown()
    srv.server_close()


def post(url, body, headers=None):
    req = urllib.request.Request(url + "/api/rpc", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health_and_ui(server):
    _, url, _ = server
    with urllib.request.urlopen(url + "/api/health") as resp:
        health = json.loads(resp.read())
    assert health["ok"] and health["auth"] == "token" and "approve" in health["ops"]
    with urllib.request.urlopen(url + "/") as resp:
        assert resp.headers["Content-Type"].startswith("text/html")
        assert b"SkillCurrent" in resp.read()


def test_rpc_auth_and_errors(server):
    _, url, tokens = server
    assert post(url, {"op": "whoami"})[0] == 401
    assert post(url, {"op": "whoami"}, {"Authorization": "Bearer ts_bad"})[0] == 401
    status, payload = post(url, {"op": "whoami"}, {"Authorization": f"Bearer {tokens['cai']}"})
    assert status == 200 and payload["result"]["member"]["handle"] == "cai"
    status, payload = post(url, {"op": "nope"}, {"Authorization": f"Bearer {tokens['cai']}"})
    assert status == 400 and payload["error"]["code"] == "invalid"
    status, payload = post(url, {"op": "whoami", "args": {"team": "other"}}, {"Authorization": f"Bearer {tokens['cai']}"})
    assert status == 403
    status, payload = post(url, {"op": "get_skill", "args": {"bogus": 1}}, {"Authorization": f"Bearer {tokens['cai']}"})
    assert status == 400 and "bad arguments" in payload["error"]["message"]
    status, payload = post(url, {"op": "add_member", "args": {"handle": "x"}}, {"Authorization": f"Bearer {tokens['cai']}"})
    assert status == 403 and payload["error"]["code"] == "forbidden"
    req = urllib.request.Request(url + "/api/rpc", data=b"not json", headers={"Authorization": f"Bearer {tokens['cai']}"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 400


def test_remote_session_end_to_end(server, tmp_path):
    from skillcurrent.installer import Installer
    from tests.conftest import skill_text

    _, url, tokens = server
    cai, ben, dee = (RemoteSession(url, token=tokens[h]) for h in ("cai", "ben", "dee"))
    cai.whoami()
    assert cai.team == "acme" and cai.actor == "cai"
    cai.call("create_skill", content=skill_text())
    assert cai.call("run_checks", slug="release-notes")["passed"]
    cai.call("submit_review", slug="release-notes", note="hello")
    with pytest.raises(Forbidden):
        cai.call("approve", slug="release-notes")
    assert ben.call("approve", slug="release-notes", release="production")["production_version"] == "1.0.0"
    with pytest.raises(Invalid):
        dee.call("list_skills", status="nah")
    inst = Installer(dee, home=tmp_path)
    assert inst.install("release-notes")["version"] == "1.0.0"
    assert inst.status()[0]["state"] == "current"
    with pytest.raises(Unauthorized):
        RemoteSession(url, token="ts_nope").whoami()


def test_no_auth_mode(service, team):
    srv = make_server(service, "127.0.0.1", 0, no_auth=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert post(url, {"op": "whoami"})[0] == 401
        status, payload = post(url, {"op": "whoami"}, {"X-SkillCurrent-Team": "acme", "X-SkillCurrent-User": "ana"})
        assert status == 200 and payload["result"]["member"]["role"] == "owner"
        session = RemoteSession(url, team="acme", actor="ben")
        assert session.call("whoami")["member"]["handle"] == "ben"
    finally:
        srv.shutdown()
        srv.server_close()
