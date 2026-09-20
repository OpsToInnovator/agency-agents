from teamskills import semver, skillfile
from tests.conftest import skill_text


def test_parse_valid_skill():
    doc = skillfile.parse(skill_text())
    assert doc.name == "release-notes"
    assert doc.tags == ["docs", "release"]
    assert doc.title == "Release Notes"
    assert doc.version is None


def test_check_reports_problems_and_warnings():
    problems, warnings, doc = skillfile.check("---\nname: Bad Name\ndescription: short\n---\nhi\n")
    assert doc is None
    assert any("lowercase" in p for p in problems)
    assert any("very short" in w for w in warnings)
    problems, _, _ = skillfile.check("# no front matter\n")
    assert "missing front matter" in problems[0]
    problems, _, _ = skillfile.check("---\nname: ok-name\ndescription: A perfectly fine description here.\n---\n\n")
    assert any("body" in p for p in problems)
    problems, _, _ = skillfile.check("---\nname: ok-name\ndescription: A perfectly fine description here.\nversion: v1\n---\n# x\n")
    assert any("MAJOR.MINOR.PATCH" in p for p in problems)
    problems, _, _ = skillfile.check("---\nname: ok-name\ndescription: A perfectly fine description here.\ntags: [Bad Tag]\n---\n# x\n")
    assert any("tag" in p for p in problems)


def test_stamp_version_preserves_extra_keys():
    text = skill_text().replace("tags: [docs, release]", "tags: [docs]\nlicense: MIT\nallowed-tools: [Read, Grep]\nmetadata:\n  author: ana")
    stamped = skillfile.stamp_version(text, "1.2.3")
    doc = skillfile.parse(stamped)
    assert doc.version == "1.2.3"
    assert doc.extra == {"license": "MIT", "allowed-tools": ["Read", "Grep"], "metadata": {"author": "ana"}}
    assert "version: 1.2.3" in stamped.split("---")[1]
    assert skillfile.stamp_version(stamped, "1.2.3") == stamped  # idempotent


def test_template_is_valid():
    problems, _warnings, doc = skillfile.check(skillfile.template("my-skill", "Does a useful thing for the team."))
    assert not problems and doc.name == "my-skill" and doc.title == "My Skill"


def test_slugify():
    assert skillfile.slugify("Backend Architect") == "backend-architect"
    assert skillfile.slugify("  AI/ML -- Engineer!! ") == "ai-ml-engineer"


def test_semver():
    assert semver.bump("1.2.3", "major") == "2.0.0"
    assert semver.bump("1.2.3", "minor") == "1.3.0"
    assert semver.bump("1.2.3", "patch") == "1.2.4"
    assert semver.compare("1.10.0", "1.9.9") == 1
    assert not semver.is_valid("01.0.0") and not semver.is_valid("1.0")
