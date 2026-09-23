"""The two beta questions, answered from data the tool records:

1. did the team keep review switched on?
2. did sync ever catch a bad copy?

Plus the landing page's terminal walkthrough, run exactly as printed."""

import html
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from skillcurrent.cli import main
from skillcurrent.installer import Installer
from tests.conftest import publish, skill_text

ROOT = Path(__file__).resolve().parent.parent


def drift_log(service, slug):
    return [(a["action"], a["details"]["response"]) for a in reversed(service.activity("acme", "ben", 100, slug, action="drift."))]


def test_status_and_sync_report_what_they_find(service, sessions, published, tmp_path):
    inst = Installer(sessions["dee"], home=tmp_path, host="laptop")
    inst.install(published)
    path = tmp_path / ".claude" / "skills" / published / "SKILL.md"

    # A hand edit: status observes it, sync keeps it (and says so), a forced sync repairs it.
    path.write_text(path.read_text() + "\nlocal tweak\n")
    inst.status()
    inst.status()  # the hook runs every session; the same finding is not repeated
    inst.sync()
    inst.sync()
    assert drift_log(service, published) == [("drift.modified", "observed"), ("drift.modified", "kept_local")]
    inst.sync(force=True)
    assert drift_log(service, published)[-1] == ("drift.modified", "forced")

    # A deleted copy: observed, then repaired by sync; the reinstall says why.
    path.unlink()
    inst.sync()
    assert drift_log(service, published)[-2:] == [("drift.missing", "observed"), ("drift.missing", "repaired")]
    installed = service.activity("acme", "ben", 5, published, action="skill.installed")[0]
    assert installed["details"]["reason"] == "sync:missing"

    # A new release: the old copy is outdated until sync brings it forward.
    service.update_draft("acme", "cai", published, skill_text(body_extra="\n## More\n\nx.\n"))
    publish(service, published)
    inst.sync()
    assert drift_log(service, published)[-2:] == [("drift.outdated", "observed"), ("drift.outdated", "repaired")]
    assert inst.status()[0]["state"] == "current"


def test_verified_receipts_stay_out_of_the_activity_log(service, sessions, published, tmp_path):
    inst = Installer(sessions["dee"], home=tmp_path, host="laptop")
    inst.install(published)
    for _ in range(5):
        inst.status()
    actions = [a["action"] for a in service.activity("acme", "ben", 100)]
    assert "receipt.verified" not in actions
    assert service.adoption("acme", "ben", slug=published)["summary"]["verified"] == 1  # still counted as evidence


def test_beta_report_answers_both_questions(service, sessions, published, tmp_path):
    first = service.beta_report("acme", "ben")
    # The fixture's single review was approved instantly and nothing has been rejected yet: flagged, not failed.
    assert first["answers"]["kept_review_on"]["verdict"] == "partly"
    assert any("no review has said no" in b for b in first["answers"]["kept_review_on"]["because"])
    # Nothing was ever installed, so no check ever ran: that must not read as "no drift".
    assert first["answers"]["sync_caught_bad_copy"]["verdict"] == "never checked"
    assert first["sync"]["checks"] == {"verified_receipts": 0, "environments_checked": 0, "days_with_checks": 0, "last_check_at": None}

    # A rejection, a second approver, and one hand-edited copy caught by sync.
    service.update_draft("acme", "cai", published, skill_text(body_extra="\n## Two\n\nx.\n"))
    service.run_checks("acme", "cai", published)
    service.submit_review("acme", "cai", published, bump="patch")
    service.reject("acme", "ben", published, reason="needs examples")
    service.submit_review("acme", "cai", published, bump="patch")
    service.approve("acme", "ana", published, release="production")
    inst = Installer(sessions["dee"], home=tmp_path, host="laptop")
    inst.install(published)
    p = tmp_path / ".claude" / "skills" / published / "SKILL.md"
    p.write_text("edited\n")
    inst.sync()

    r = service.beta_report("acme", "ben", days=30)
    assert r["review"]["submitted"] == 3 and r["review"]["approved"] == 2 and r["review"]["rejected"] == 1
    assert r["review"]["distinct_approvers"] == 2 and r["review"]["draft_installs"] == 0
    assert r["answers"]["kept_review_on"]["verdict"] == "yes"  # reviews say no sometimes, two approvers, no draft installs
    assert r["sync"]["by_state"]["modified"] == 1 and r["sync"]["by_response"]["kept_local"] == 1
    assert r["sync"]["bad_copies"] == 1 and r["sync"]["checks"]["environments_checked"] == 1
    assert r["answers"]["sync_caught_bad_copy"]["verdict"] == "yes"
    blob = repr(r)
    assert "dee" not in blob and "cai" not in blob and "release-notes" not in blob  # counts only, no names or content


def test_beta_report_tells_clean_checks_from_no_checks(service, sessions, published, tmp_path):
    inst = Installer(sessions["dee"], home=tmp_path, host="laptop")
    inst.install(published)
    assert service.beta_report("acme", "ben")["answers"]["sync_caught_bad_copy"]["verdict"] == "never checked"
    inst.status()
    inst.sync()
    r = service.beta_report("acme", "ben")
    assert r["answers"]["sync_caught_bad_copy"]["verdict"] == "no bad copy seen"
    assert r["sync"]["checks"]["verified_receipts"] == 2 and r["sync"]["checks"]["environments_checked"] == 1
    assert r["sync"]["checks"]["days_with_checks"] == 1 and r["sync"]["checks"]["last_check_at"]

    # A new release leaves the copy outdated: counted, and brought forward, but not a bad copy.
    service.update_draft("acme", "cai", published, skill_text(body_extra="\n## Next\n\nx.\n"))
    publish(service, published)
    inst.sync()
    r = service.beta_report("acme", "ben")
    assert r["sync"]["by_state"]["outdated"] == 1 and r["sync"]["bad_copies"] == 0
    assert r["answers"]["sync_caught_bad_copy"]["verdict"] == "no bad copy seen"
    assert any("1 outdated copy" in b for b in r["answers"]["sync_caught_bad_copy"]["because"])

    # A deleted copy is a bad copy.
    (tmp_path / ".claude" / "skills" / published / "SKILL.md").unlink()
    inst.sync()
    r = service.beta_report("acme", "ben")
    assert r["sync"]["by_state"]["missing"] == 1 and r["sync"]["bad_copies"] == 1
    assert r["answers"]["sync_caught_bad_copy"]["verdict"] == "yes"
    assert any("repaired 2" in b for b in r["answers"]["sync_caught_bad_copy"]["because"])


def test_beta_report_flags_draft_installs_and_no_auth(service, sessions, published, tmp_path):
    service.update_draft("acme", "cai", published, skill_text(body_extra="\n## Wip\n\nx.\n"))
    Installer(sessions["dee"], home=tmp_path, host="laptop").install(published, ref="draft")
    service.auth_mode = "none"
    r = service.beta_report("acme", "ben")
    assert r["review"]["draft_installs"] == 1 and r["auth_mode"] == "none"
    assert r["answers"]["kept_review_on"]["verdict"] == "partly"
    assert any("--no-auth" in b for b in r["answers"]["kept_review_on"]["because"])


def test_beta_report_cli(tmp_path, capsys):
    db = str(tmp_path / "db.sqlite")
    main(["--db", db, "init", "--team", "acme", "--owner", "ana"])
    capsys.readouterr()
    assert main(["--db", db, "--team", "acme", "--as", "ana", "beta-report", "--days", "7"]) == 0
    out = capsys.readouterr().out
    assert "1. Did the team keep review switched on?  NO ACTIVITY" in out and "2. Did sync ever catch a bad copy?  NEVER CHECKED" in out
    assert "checks: 0 environment(s), 0 day(s), 0 verified receipt(s), last never" in out
    assert main(["--db", db, "--team", "acme", "--as", "ana", "beta-report", "--days", "0"]) == 1


def walkthrough_commands() -> list[tuple[str, list[str]]]:
    """(command, expected output lines) pairs from the page's terminal block."""
    page = (ROOT / "skillcurrent" / "web" / "beta.html").read_text(encoding="utf-8")
    block = re.search(r'<pre id="walkthrough">(.*?)</pre>', page, re.S).group(1)
    text = html.unescape(re.sub(r"<[^>]+>", "", block))
    steps: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if line.startswith("$ "):
            steps.append((line[2:], []))
        else:
            steps[-1][1].append(line)
    return steps


def test_landing_walkthrough_runs_as_printed(tmp_path):
    """Every command on the page runs, in order, and every output line shown is real."""
    shutil.copytree(ROOT / "tests" / "fixtures" / "refund-handoff", tmp_path / "refund-handoff")
    env = {**os.environ, "PYTHONPATH": str(ROOT), "SKILLCURRENT_DB": str(tmp_path / "db.sqlite"),
           "SKILLCURRENT_HOME": str(tmp_path / "home"), "SKILLCURRENT_HOST": "laptop"}
    env.pop("SKILLCURRENT_TEAM", None)
    env.pop("SKILLCURRENT_USER", None)
    steps = walkthrough_commands()
    assert len(steps) >= 9
    for command, expected in steps:
        if command.startswith("export "):
            for assignment in shlex.split(command)[1:]:
                k, v = assignment.split("=", 1)
                env[k] = v
            continue
        argv = shlex.split(command)
        assert argv[0] == "skillcurrent"
        run = subprocess.run([sys.executable, "-m", "skillcurrent", *argv[1:]], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
        assert run.returncode == 0, f"{command!r} failed: {run.stderr}"
        flat = " ".join(run.stdout.split())
        for line in expected:
            want = " ".join(line.rstrip("…").split())
            assert want in flat, f"{command!r} did not print {line!r}; got {run.stdout!r}"


def test_sync_repairs_copies_where_they_were_installed(service, sessions, published, tmp_path, monkeypatch):
    """The session-start hook runs a bare `sync` from wherever the tool starts: it must repair each copy
    at its recorded path, not recompute a folder from the hook's own flags and working directory."""
    custom = tmp_path / "cursor-skills"
    project = tmp_path / "proj"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    Installer(sessions["dee"], home=tmp_path, custom_dir=custom, host="laptop").install(published, "custom")
    Installer(sessions["dee"], home=tmp_path, project=project, host="laptop").install(published, "claude-code-project")
    (custom / published / "SKILL.md").unlink()
    (project / ".claude" / "skills" / published / "SKILL.md").unlink()

    monkeypatch.chdir(elsewhere)
    hook = Installer(sessions["dee"], home=tmp_path, host="laptop")  # no --dir, no --project
    actions = {a["target"]: a["action"] for a in hook.sync()}
    assert actions == {"custom": "updated", "claude-code-project": "updated"}
    assert (custom / published / "SKILL.md").is_file()
    assert (project / ".claude" / "skills" / published / "SKILL.md").is_file()
    assert not (elsewhere / ".claude").exists()
    assert {s["target"]: s["state"] for s in hook.status()} == {"custom": "current", "claude-code-project": "current"}


def test_sync_never_writes_outside_a_skill_folder(service, sessions, published, tmp_path):
    """A recorded path comes from the server; sync must refuse one that is not <slug>/SKILL.md."""
    inst = Installer(sessions["dee"], home=tmp_path, host="laptop")
    inst.install(published)
    victim = tmp_path / "dotfile"
    victim.write_text("keep me\n")
    row = service.list_installs("acme", "dee", host="laptop")[0]
    service.record_install("acme", "dee", published, row["version"], row["target"], str(victim), "0" * 64, host="laptop")
    actions = inst.sync(force=True)
    assert actions[0]["action"] == "skipped" and "SKILL.md" in actions[0]["reason"]
    assert victim.read_text() == "keep me\n"
