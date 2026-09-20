import json

import pytest

from teamskills.cli import main
from tests.conftest import skill_text


def run(capsys, *argv):
    code = main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMSKILLS_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TEAMSKILLS_TEAM", "acme")
    monkeypatch.setenv("TEAMSKILLS_USER", "ana")
    monkeypatch.setenv("TEAMSKILLS_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_full_cli_workflow(env, capsys):
    code, out, _ = run(capsys, "init", "--team", "acme", "--name", "Acme", "--owner", "ana")
    assert code == 0 and "Owner token" in out
    assert run(capsys, "members", "add", "ben", "--role", "maintainer")[0] == 0
    assert run(capsys, "members", "add", "cai", "--role", "contributor")[0] == 0
    code, out, _ = run(capsys, "--json", "members")
    assert [m["handle"] for m in json.loads(out)] == ["ana", "ben", "cai"]

    skill_file = env / "SKILL.md"
    skill_file.write_text(skill_text(), encoding="utf-8")
    assert run(capsys, "skills", "validate", skill_file)[0] == 0
    bad = env / "bad.md"
    bad.write_text("---\nname: Bad\n---\n", encoding="utf-8")
    code, out, _ = run(capsys, "skills", "validate", bad)
    assert code == 1 and "INVALID" in out

    code, out, _ = run(capsys, "--as", "cai", "skills", "new", skill_file)
    assert code == 0 and "Created draft release-notes" in out
    code, out, _ = run(capsys, "--as", "cai", "skills", "new", "--name", "pr-review", "--description", "Review pull requests the team way.")
    assert code == 0
    code, out, _ = run(capsys, "--as", "cai", "skills", "submit", "release-notes", "--note", "first")
    assert code == 0 and "version 1.0.0" in out
    code, out, err = run(capsys, "--as", "cai", "skills", "approve", "release-notes")
    assert code == 1 and "forbidden" in err
    code, out, _ = run(capsys, "--as", "ben", "skills", "approve", "release-notes")
    assert code == 0 and "Published release-notes 1.0.0" in out
    code, out, _ = run(capsys, "skills", "list", "--status", "published")
    assert "release-notes" in out and "pr-review" not in out
    code, out, _ = run(capsys, "skills", "cat", "release-notes")
    assert out.startswith("---\nname: release-notes")

    code, out, _ = run(capsys, "--as", "cai", "install", "release-notes")
    assert code == 0 and "Installed release-notes 1.0.0" in out
    assert (env / "home" / ".claude" / "skills" / "release-notes" / "SKILL.md").exists()
    code, out, _ = run(capsys, "--as", "cai", "install", "release-notes", "--target", "claude-code-project", "--project", env / "proj")
    assert (env / "proj" / ".claude" / "skills" / "release-notes" / "SKILL.md").exists()
    assert run(capsys, "--as", "cai", "status")[0] == 0

    skill_file.write_text(skill_text(body_extra="\n## More\n\nMore text.\n"), encoding="utf-8")
    assert run(capsys, "--as", "cai", "skills", "edit", "release-notes", skill_file)[0] == 0
    code, out, _ = run(capsys, "--as", "cai", "skills", "diff", "release-notes")
    assert "+## More" in out
    assert run(capsys, "--as", "cai", "skills", "submit", "release-notes", "--bump", "minor")[0] == 0
    code, out, _ = run(capsys, "reviews")
    assert "1.1.0" in out
    assert run(capsys, "--as", "ben", "skills", "reject", "release-notes", "--reason", "add examples")[0] == 0
    assert run(capsys, "--as", "cai", "skills", "submit", "release-notes", "--bump", "minor")[0] == 0
    assert run(capsys, "--as", "ana", "skills", "approve", "release-notes")[0] == 0
    code, out, _ = run(capsys, "--as", "cai", "status")
    assert code == 2 and out.count("outdated") == 2
    code, out, _ = run(capsys, "--as", "cai", "sync")
    assert code == 0 and out.count("updated") == 2
    assert run(capsys, "--as", "cai", "status")[0] == 0
    code, out, _ = run(capsys, "--as", "cai", "uninstall", "release-notes", "--target", "claude-code-project", "--project", env / "proj")
    assert code == 0

    assert run(capsys, "--as", "ben", "skills", "deprecate", "release-notes", "--reason", "replaced")[0] == 0
    code, out, _ = run(capsys, "--as", "cai", "status")
    assert "deprecated" in out
    assert run(capsys, "--as", "ben", "skills", "restore", "release-notes")[0] == 0
    code, out, _ = run(capsys, "skills", "versions", "release-notes")
    assert "1.1.0" in out and "1.0.0" in out
    code, out, _ = run(capsys, "dashboard")
    assert "published 1" in out and "drafts 1" in out
    code, out, _ = run(capsys, "activity", "--limit", "5")
    assert "skill.restored" in out
    code, out, _ = run(capsys, "targets")
    assert "claude-code" in out and "osaurus" in out
    code, out, _ = run(capsys, "--json", "skills", "show", "release-notes")
    assert json.loads(out)["latest_version"] == "1.1.0"


def test_cli_errors(env, capsys):
    code, _, err = run(capsys, "skills", "list")
    assert code == 1 and "not_found" in err  # no team yet
    run(capsys, "init", "--team", "acme", "--owner", "ana")
    code, _, err = run(capsys, "--as", "ghost", "skills", "list")
    assert code == 1 and "forbidden" in err
    code, _, err = run(capsys, "--json", "skills", "show", "missing")
    assert code == 1
    code, _, err = run(capsys, "skills", "new", env / "nope.md")
    assert code == 1 and "No such file" in err


def test_cli_import(env, capsys):
    run(capsys, "init", "--team", "acme", "--owner", "ana")
    (env / "agent.md").write_text("---\nname: Reality Checker\ndescription: Verifies a feature is production ready before release.\ncolor: red\n---\n# Reality Checker\n\nCheck everything twice before declaring victory.\n", encoding="utf-8")
    code, out, _ = run(capsys, "import", env)
    assert code == 0 and "created" in out and "reality-checker" in out
