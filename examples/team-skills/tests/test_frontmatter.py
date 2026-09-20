import pytest

from teamskills import frontmatter as fm


def test_split_and_parse_scalars_lists_and_mappings():
    text = """---
name: backend-architect
description: "Senior backend architect: designs systems"
color: blue
emoji: 🏗️
count: 3
flag: yes
tags: [docs, "release notes", release]
services:
  - name: Service One
    url: https://one.example
    tier: free
  - name: Two
    url: https://two.example
metadata:
  author: ana
  level: 2
notes: |
  first line
  second line
---
# Body
"""
    meta_text, body = fm.split(text)
    meta = fm.parse(meta_text)
    assert meta["name"] == "backend-architect"
    assert meta["description"] == "Senior backend architect: designs systems"
    assert meta["emoji"] == "🏗️"
    assert meta["count"] == 3 and meta["flag"] is True
    assert meta["tags"] == ["docs", "release notes", "release"]
    assert meta["services"][0] == {"name": "Service One", "url": "https://one.example", "tier": "free"}
    assert meta["services"][1]["url"] == "https://two.example"
    assert meta["metadata"] == {"author": "ana", "level": 2}
    assert meta["notes"] == "first line\nsecond line"
    assert body == "# Body\n"


def test_block_list_of_scalars_and_comments():
    meta = fm.parse("tags:\n  - one   # trailing comment\n  - two\n# full comment\nurl: https://x.example/#frag\n")
    assert meta["tags"] == ["one", "two"]
    assert meta["url"] == "https://x.example/#frag"


def test_no_front_matter_and_unclosed():
    assert fm.split("# just markdown\n") == (None, "# just markdown\n")
    with pytest.raises(fm.FrontMatterError):
        fm.split("---\nname: x\n# never closed\n")


def test_parse_errors():
    with pytest.raises(fm.FrontMatterError):
        fm.parse("  indented: no\n")
    with pytest.raises(fm.FrontMatterError):
        fm.parse("not a key value line\n")
    with pytest.raises(fm.FrontMatterError):
        fm.parse("tags: [unclosed\n")


def test_dump_round_trips():
    meta = {
        "name": "x", "description": "Has: colon and #hash", "version": "1.0.0", "tags": ["a", "b"],
        "flag": True, "n": 0, "empty": "", "yesish": "yes", "metadata": {"k": "v"},
        "services": [{"name": "S", "tier": "free"}], "multi": "line one\nline two",
    }
    dumped = fm.dump(meta)
    assert fm.parse(dumped) == meta


def test_render_places_body_after_blank_line():
    out = fm.render({"name": "x", "description": "d"}, "# T\n")
    assert out == "---\nname: x\ndescription: d\n---\n\n# T\n"
