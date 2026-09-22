import pytest

from skillcurrent.errors import Invalid, NotFound
from skillcurrent.installer import TARGETS, Installer
from tests.conftest import publish, skill_text


def test_install_status_sync(service, sessions, published, tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    inst = Installer(sessions["dee"], home=home, project=project, host="laptop")
    r = inst.install(published)
    assert r["path"] == str(home / ".claude" / "skills" / published / "SKILL.md") and r["version"] == "1.0.0"
    assert r["host"] == "laptop" and r["channel"] == "production"
    inst.install(published, "claude-code-project")
    with pytest.raises(Invalid):
        inst.install(published, "custom")  # needs --dir
    with pytest.raises(Invalid):
        inst.install(published, "nope")
    assert (project / ".claude" / "skills" / published / "SKILL.md").exists()
    assert {s["state"] for s in inst.status()} == {"current"}
    # Intact copies send 'verified' receipts, which the adoption view reports.
    adoption = service.adoption("acme", "dee", slug=published)
    assert adoption["summary"]["environments"] == 2 and adoption["summary"]["verified"] == 2
    assert {e["environment"] for e in adoption["environments"]} == {"dee@laptop"}

    # Approve 1.1.0 but only release it to canary: production installs stay current.
    service.update_draft("acme", "cai", published, skill_text(body_extra="\n## New\n\nx.\n"))
    publish(service, published, release="canary")
    assert {s["state"] for s in inst.status()} == {"current"}
    other = Installer(sessions["cai"], home=tmp_path / "cai", host="desk")
    assert other.install(published, channel="canary")["version"] == "1.1.0"
    assert other.status()[0]["state"] == "current"

    # Promote to production; modify the project copy locally, delete the global copy.
    service.release("acme", "ben", published, "1.1.0")
    (project / ".claude" / "skills" / published / "SKILL.md").write_text("tweaked", encoding="utf-8")
    (home / ".claude" / "skills" / published / "SKILL.md").unlink()
    states = {s["target"]: s["state"] for s in inst.status()}
    assert states == {"claude-code": "missing", "claude-code-project": "modified"}

    actions = {a["target"]: a["action"] for a in inst.sync()}
    assert actions == {"claude-code": "updated", "claude-code-project": "skipped"}
    actions = {a["target"]: a["action"] for a in inst.sync(force=True)}
    assert actions == {"claude-code": "kept", "claude-code-project": "updated"}
    assert {s["installed"] for s in inst.status()} == {"1.1.0"}

    # Rollback moves the channel; installs are stale until sync.
    service.rollback("acme", "ben", published, reason="regression")
    assert {s["state"] for s in inst.status()} == {"outdated"}
    assert {a["installed"] for a in inst.sync()} == {"1.0.0"}
    service.release("acme", "ben", published, "1.1.0")
    inst.sync()

    service.deprecate("acme", "ben", published, "old")
    assert {s["state"] for s in inst.status()} == {"deprecated"}
    assert {a["action"] for a in inst.sync()} == {"kept"}

    assert inst.uninstall(published)["removed_file"] is True
    assert not (home / ".claude" / "skills" / published).exists()
    with pytest.raises(NotFound):
        inst.uninstall(published)
    assert [s["target"] for s in inst.status()] == ["claude-code-project"]


def test_target_directories(tmp_path):
    assert TARGETS["antigravity"].directory(home=tmp_path) == (tmp_path / ".gemini/config/skills").resolve()
    assert TARGETS["antigravity-project"].directory(project=tmp_path) == (tmp_path / ".agents/skills").resolve()
    assert TARGETS["custom"].directory(custom_dir=tmp_path / "x") == (tmp_path / "x").resolve()


def test_env_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLCURRENT_HOME", str(tmp_path))
    assert TARGETS["codex"].directory() == (tmp_path / ".agents/skills").resolve()


def test_report_receipts_and_adoption(service, sessions, published, tmp_path):
    inst = Installer(sessions["dee"], home=tmp_path, host="laptop")
    inst.install(published)
    inst.report(published, "loaded", detail="hook: session start")
    a = service.adoption("acme", "ben", slug=published)
    env = a["environments"][0]
    assert env["evidence"] == "loaded" and env["state"] == "current" and env["runtime"] == "claude-code"
    assert a["summary"]["loaded"] == 1 and a["summary"]["task_tested"] == 0
    inst.report(published, "task_tested", detail="fixture: refund handoff")
    assert service.adoption("acme", "ben", slug=published)["environments"][0]["evidence"] == "task_tested"
    with pytest.raises(Invalid):
        inst.report(published, "installed")
    with pytest.raises(Invalid):
        service.report("acme", "dee", published, "loaded", target="codex", host="laptop")  # not installed there
    # A receipt for older bytes does not count once a newer version is installed.
    service.update_draft("acme", "cai", published, skill_text(body_extra="\n## New\n\nx.\n"))
    publish(service, published)
    inst.sync()
    assert service.adoption("acme", "ben", slug=published)["environments"][0]["evidence"] == "installed"
    inst.status()  # re-hashes the new bytes and sends a fresh 'verified' receipt
    assert service.adoption("acme", "ben", slug=published)["environments"][0]["evidence"] == "verified"
