from skillcurrent import checks, skillfile
from tests.conftest import skill_text


def by_id(report):
    return {r["id"]: r for r in report["results"]}


def test_builtin_checks_pass_on_a_good_skill():
    report = checks.run(skill_text())
    assert report["passed"] and report["failed"] == 0
    assert report["content_hash"] == skillfile.content_hash(skill_text())
    assert {r["category"] for r in report["results"]} <= set(checks.CATEGORIES)
    assert all(r["rule"] for r in report["results"])  # every check explains its rule


def test_template_fails_placeholder_and_description_checks():
    report = checks.run(skillfile.template("my-skill"))
    ids = by_id(report)
    assert not report["passed"]
    assert not ids["placeholders"]["passed"] and not ids["description"]["passed"]
    assert ids["sections"]["passed"]


def test_secret_and_path_checks():
    text = skill_text(body_extra="\nUse token ghp_abcdefghijklmnopqrstuvwxyz0123456789 and read /Users/jake/notes.md.\n")
    ids = by_id(checks.run(text))
    assert not ids["secrets"]["passed"] and "GitHub token" in ids["secrets"]["detail"]
    assert not ids["portable"]["passed"] and "/Users/jake" in ids["portable"]["detail"]
    ids = by_id(checks.run(skill_text(body_extra="\nSee https://example.com/home/page for details.\n")))
    assert ids["portable"]["passed"]  # a URL path is not a machine path


def test_sections_and_actionable_checks():
    text = "---\nname: flat\ndescription: A skill with no structure at all, just prose that goes on.\n---\n# Flat\n\nJust do the thing and report back when it is done.\n"
    ids = by_id(checks.run(text))
    assert not ids["sections"]["passed"] and not ids["actionable"]["passed"]
    assert ids["front-matter"]["passed"]


def test_invalid_file_only_reports_front_matter():
    report = checks.run("no front matter here")
    assert not report["passed"] and [r["id"] for r in report["results"]] == ["front-matter"]


def test_team_rules():
    rules = [
        {"name": "inspect-first", "kind": "require", "pattern": r"inspect .* before", "category": "evidence", "rationale": "Inspect what the agent can already see."},
        {"name": "no-screenshot-first", "kind": "forbid", "pattern": r"ask for a screenshot", "category": "evidence", "rationale": ""},
        {"name": "guardrails-section", "kind": "section", "pattern": r"guardrails|boundar", "category": "authority", "rationale": ""},
        {"name": "broken", "kind": "require", "pattern": r"(", "category": "team", "rationale": ""},
    ]
    text = skill_text(body_extra="\n## Guardrails\n\n- Inspect the connectors before you ask for a screenshot.\n")
    ids = by_id(checks.run(text, rules))
    assert ids["rule:inspect-first"]["passed"]
    assert not ids["rule:no-screenshot-first"]["passed"]
    assert ids["rule:guardrails-section"]["passed"]
    assert not ids["rule:broken"]["passed"] and "invalid pattern" in ids["rule:broken"]["detail"]
    assert "Inspect what the agent" in ids["rule:inspect-first"]["rule"]
