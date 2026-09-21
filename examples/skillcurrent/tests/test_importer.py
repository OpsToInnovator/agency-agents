from skillcurrent import importer

AGENT = """---
name: Backend Architect
description: Senior backend architect specializing in scalable system design and cloud infrastructure.
color: blue
emoji: 🏗️
vibe: Designs the systems that hold everything up.
services:
  - name: Some Service
    url: https://example.com
    tier: free
---

# Backend Architect Agent Personality

You are **Backend Architect**.

## 🧠 Your Identity & Memory
- **Role**: System architecture and server-side development specialist
"""


def test_normalize_agent_file_into_skill():
    content, problems = importer.normalize(AGENT)
    assert not problems
    assert content.startswith("---\nname: backend-architect\ndescription: Senior backend architect")
    assert "color:" not in content and "services:" not in content
    assert "# Backend Architect Agent Personality" in content


def test_import_directory_creates_updates_and_skips(sessions, tmp_path):
    (tmp_path / "engineering-backend-architect.md").write_text(AGENT, encoding="utf-8")
    (tmp_path / "README.md").write_text("# not a skill\n", encoding="utf-8")
    (tmp_path / "broken.md").write_text("no front matter\n", encoding="utf-8")
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: my-skill\ndescription: A directory-style skill for testing imports.\n---\n# My Skill\n\nDo the thing carefully and well.\n", encoding="utf-8")

    results = importer.import_path(sessions["ana"], tmp_path)
    by_source = {r["source"].rsplit("/", 1)[-1] if not r["source"].endswith("SKILL.md") else "SKILL.md": r for r in results}
    assert by_source["engineering-backend-architect.md"]["action"] == "created"
    assert by_source["SKILL.md"]["action"] == "created"
    assert by_source["broken.md"]["action"] == "skipped"
    assert "README.md" not in by_source

    again = importer.import_path(sessions["ana"], tmp_path)
    assert {r["action"] for r in again if r.get("slug")} == {"skipped"}
    updated = importer.import_path(sessions["ana"], tmp_path, update=True)
    assert {r["action"] for r in updated if r.get("slug")} == {"updated"}
    viewer = importer.import_path(sessions["dee"], tmp_path / "my-skill" / "SKILL.md")
    assert viewer[0]["action"] == "skipped" and "contributor" in viewer[0]["reason"]
