import pytest

from teamskills.errors import Invalid, NotFound
from teamskills.installer import TARGETS, Installer
from tests.conftest import skill_text


def test_install_status_sync(service, sessions, published, tmp_path):
    home, project = tmp_path / "home", tmp_path / "proj"
    inst = Installer(sessions["dee"], home=home, project=project)
    r = inst.install(published)
    assert r["path"] == str(home / ".claude" / "skills" / published / "SKILL.md") and r["version"] == "1.0.0"
    inst.install(published, "claude-code-project")
    with pytest.raises(Invalid):
        inst.install(published, "custom")  # needs --dir
    with pytest.raises(Invalid):
        inst.install(published, "nope")
    assert (project / ".claude" / "skills" / published / "SKILL.md").exists()
    assert {s["state"] for s in inst.status()} == {"current"}

    # Publish 1.1.0, modify the project copy locally, delete the global copy.
    service.update_draft("acme", "cai", published, skill_text(body_extra="\n## New\n\nx.\n"))
    service.submit_review("acme", "cai", published, bump="minor")
    service.approve("acme", "ben", published)
    (project / ".claude" / "skills" / published / "SKILL.md").write_text("tweaked", encoding="utf-8")
    (home / ".claude" / "skills" / published / "SKILL.md").unlink()
    states = {s["target"]: s["state"] for s in inst.status()}
    assert states == {"claude-code": "missing", "claude-code-project": "modified"}

    actions = {a["target"]: a["action"] for a in inst.sync()}
    assert actions == {"claude-code": "updated", "claude-code-project": "skipped"}
    actions = {a["target"]: a["action"] for a in inst.sync(force=True)}
    assert actions == {"claude-code": "kept", "claude-code-project": "updated"}
    assert {s["installed"] for s in inst.status()} == {"1.1.0"}

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
    monkeypatch.setenv("TEAMSKILLS_HOME", str(tmp_path))
    assert TARGETS["codex"].directory() == (tmp_path / ".codex/skills").resolve()
