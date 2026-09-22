import pytest

from skillcurrent import skillfile
from skillcurrent.service import Service
from skillcurrent.session import LocalSession
from skillcurrent.store import Store


SKILL = """---
name: release-notes
description: Write release notes from merged pull requests in the team's house style.
tags: [docs, release]
---

# Release Notes

## When to use this skill

When a release is cut and the changelog needs to be written for humans.

## Steps

1. List merged PRs since the last tag.
2. Group them by user-facing impact.
3. Write one line per change, active voice.
"""


def skill_text(name="release-notes", body_extra="", description=None):
    text = SKILL.replace("release-notes", name)
    if description:
        text = text.replace("Write release notes from merged pull requests in the team's house style.", description)
    return text + body_extra


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    yield s
    s.close()


@pytest.fixture
def service(store):
    return Service(store)


@pytest.fixture
def team(service):
    """A team with an owner (ana), a maintainer (ben), a contributor (cai) and a viewer (dee)."""
    created = service.create_team("acme", "Acme", "ana", "Ana")
    tokens = {"ana": created["token"]}
    for handle, role in (("ben", "maintainer"), ("cai", "contributor"), ("dee", "viewer")):
        tokens[handle] = service.add_member("acme", "ana", handle, role)["token"]
    return {"slug": "acme", "tokens": tokens}


@pytest.fixture
def sessions(service, team):
    return {h: LocalSession(service, "acme", h) for h in ("ana", "ben", "cai", "dee")}


def publish(service, slug, bump="minor", note="", release="production", editor="cai", approver="ben"):
    """Run checks, submit, approve and release the current draft of ``slug``."""
    service.run_checks("acme", editor, slug)
    service.submit_review("acme", editor, slug, bump=bump, note=note)
    return service.approve("acme", approver, slug, release=release)


@pytest.fixture
def published(service, team):
    """release-notes 1.0.0 approved and released to production (drafted by cai, approved by ben)."""
    service.create_skill("acme", "cai", skill_text())
    publish(service, "release-notes", note="first cut")
    return "release-notes"


__all__ = ["skill_text", "skillfile", "publish"]
