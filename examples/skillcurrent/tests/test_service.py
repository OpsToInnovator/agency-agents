import pytest

from skillcurrent.errors import Conflict, Forbidden, Invalid, NotFound
from skillcurrent import skillfile
from tests.conftest import publish, skill_text


def test_create_team_and_authenticate(service):
    created = service.create_team("acme", "Acme", "ana")
    assert created["token"].startswith("ts_")
    assert service.authenticate(created["token"]) == ("acme", "ana")
    assert service.authenticate("ts_bogus") is None
    assert service.authenticate("") is None
    with pytest.raises(Conflict):
        service.create_team("acme", "Again", "bob")
    with pytest.raises(Invalid):
        service.create_team("Bad Slug", "x", "bob")
    with pytest.raises(Invalid):
        service.create_team("ok", "x", "Bad Handle")


def test_member_management_rules(service, team):
    with pytest.raises(Forbidden):
        service.add_member("acme", "ben", "eve")  # maintainers cannot add
    with pytest.raises(Conflict):
        service.add_member("acme", "ana", "ben")
    with pytest.raises(Invalid):
        service.add_member("acme", "ana", "eve", role="king")
    with pytest.raises(Conflict):
        service.set_role("acme", "ana", "ana", "viewer")  # last owner
    with pytest.raises(Conflict):
        service.remove_member("acme", "ana", "ana")
    service.set_role("acme", "ana", "ben", "owner")
    service.set_role("acme", "ben", "ana", "maintainer")  # now allowed: ben is an owner
    assert service.whoami("acme", "ana")["member"]["role"] == "maintainer"
    with pytest.raises(Forbidden):
        service.whoami("acme", "nobody")
    with pytest.raises(NotFound):
        service.whoami("nope", "ana")


def test_token_rotation(service, team):
    old = team["tokens"]["cai"]
    new = service.rotate_token("acme", "cai")["token"]
    assert service.authenticate(old) is None and service.authenticate(new) == ("acme", "cai")
    with pytest.raises(Forbidden):
        service.rotate_token("acme", "cai", handle="ben")
    assert service.rotate_token("acme", "ana", handle="ben")["handle"] == "ben"


def test_draft_review_publish_workflow(service, team):
    with pytest.raises(Forbidden):
        service.create_skill("acme", "dee", skill_text())  # viewers cannot create
    view = service.create_skill("acme", "cai", skill_text())
    assert view["status"] == "draft" and view["has_draft"] and view["owner"] == "cai"
    with pytest.raises(Conflict):
        service.create_skill("acme", "cai", skill_text())
    with pytest.raises(Invalid):
        service.create_skill("acme", "cai", "no front matter")

    # Only the owner (contributor) or maintainers can edit; the slug is fixed.
    with pytest.raises(Forbidden):
        service.update_draft("acme", "dee", "release-notes", skill_text())
    with pytest.raises(Invalid):
        service.update_draft("acme", "cai", "release-notes", skill_text(name="other-name"))
    service.update_draft("acme", "cai", "release-notes", skill_text(body_extra="\n## More\n\nText.\n"))

    # Submission needs a passing check run against the exact draft bytes.
    with pytest.raises(Conflict, match="run the checks"):
        service.submit_review("acme", "cai", "release-notes")
    report = service.run_checks("acme", "cai", "release-notes")
    assert report["passed"] and report["total"] >= 7
    service.update_draft("acme", "cai", "release-notes", skill_text(body_extra="\n## More\n\nOther text.\n"))
    assert service.get_skill("acme", "cai", "release-notes")["checks"]["current"] is False
    with pytest.raises(Conflict, match="run the checks"):
        service.submit_review("acme", "cai", "release-notes")
    service.run_checks("acme", "cai", "release-notes")

    review = service.submit_review("acme", "cai", "release-notes", bump="major", note="v1")
    assert review["proposed_version"] == "1.0.0"  # first release ignores the bump
    assert service.get_skill("acme", "dee", "release-notes")["status"] == "in_review"
    with pytest.raises(Conflict):
        service.submit_review("acme", "cai", "release-notes")
    with pytest.raises(Conflict):
        service.update_draft("acme", "cai", "release-notes", skill_text())  # frozen while in review
    with pytest.raises(Forbidden):
        service.approve("acme", "cai", "release-notes")  # not a maintainer
    with pytest.raises(Forbidden):
        service.approve("acme", "dee", "release-notes")

    # A maintainer cannot approve their own submission.
    service.withdraw_review("acme", "cai", "release-notes")
    service.submit_review("acme", "ben", "release-notes", note="ben submits")
    with pytest.raises(Forbidden):
        service.approve("acme", "ben", "release-notes")
    out = service.approve("acme", "ana", "release-notes", comment="ship it")
    assert out["status"] == "approved" and out["latest_version"] == "1.0.0" and not out["has_draft"]
    assert out["approved"]["author"] == "ben" and out["approved"]["approved_by"] == "ana"
    assert out["channels"] == {} and out["production_version"] is None
    with pytest.raises(NotFound, match="no release on the production channel"):
        service.get_content("acme", "dee", "release-notes")  # approved is not released
    assert service.get_content("acme", "dee", "release-notes", ref="latest")["version"] == "1.0.0"

    rel = service.release("acme", "ben", "release-notes")
    assert rel["version"] == "1.0.0" and rel["previous"] is None and rel["channel"] == "production"
    content = service.get_content("acme", "dee", "release-notes")
    assert "version: 1.0.0" in content["content"] and content["ref"] == "production" and content["channel"] == "production"
    assert service.get_skill("acme", "dee", "release-notes")["status"] == "published"

    # Second iteration: patch bump, reject, then approve.
    service.update_draft("acme", "cai", "release-notes", skill_text(body_extra="\n## Even more\n\nText.\n"))
    assert service.get_skill("acme", "cai", "release-notes")["status"] == "published"  # draft overlays a published skill
    assert "+## Even more" in service.diff("acme", "cai", "release-notes")["diff"]
    service.run_checks("acme", "cai", "release-notes")
    service.submit_review("acme", "cai", "release-notes", bump="patch")
    with pytest.raises(Invalid):
        service.reject("acme", "ben", "release-notes", reason="  ")
    service.reject("acme", "ben", "release-notes", reason="needs examples")
    skill = service.get_skill("acme", "cai", "release-notes")
    assert skill["status"] == "published" and skill["has_draft"] and skill["reviews"][0]["decision"] == "rejected"
    service.submit_review("acme", "cai", "release-notes", bump="patch")  # the check run still matches these bytes
    assert service.approve("acme", "ben", "release-notes", release="production")["production_version"] == "1.0.1"
    versions = [v["version"] for v in service.list_versions("acme", "dee", "release-notes")]
    assert versions == ["1.0.1", "1.0.0"]
    assert service.get_content("acme", "dee", "release-notes", ref="1.0.0")["version"] == "1.0.0"
    with pytest.raises(NotFound):
        service.get_content("acme", "dee", "release-notes", ref="9.9.9")

    # Discarding a draft restores the published metadata.
    service.update_draft("acme", "cai", "release-notes", skill_text(description="Changed description for the draft only."))
    assert service.get_skill("acme", "cai", "release-notes")["description"].startswith("Changed")
    service.discard_draft("acme", "cai", "release-notes")
    assert service.get_skill("acme", "cai", "release-notes")["description"].startswith("Write release notes")


def test_deprecate_restore_delete(service, team, published):
    with pytest.raises(Forbidden):
        service.deprecate("acme", "cai", published)
    with pytest.raises(Conflict):
        service.delete_skill("acme", "ben", published)  # published skills cannot be deleted
    view = service.deprecate("acme", "ben", published, reason="superseded")
    assert view["status"] == "deprecated" and view["deprecation_reason"] == "superseded"
    with pytest.raises(Conflict):
        service.deprecate("acme", "ben", published)
    assert service.restore("acme", "ben", published)["status"] == "published"

    service.create_skill("acme", "cai", skill_text(name="scratch"))
    with pytest.raises(Invalid):
        service.deprecate("acme", "ben", "scratch")  # never published
    with pytest.raises(Conflict):
        service.discard_draft("acme", "cai", "scratch")
    with pytest.raises(Forbidden):
        service.delete_skill("acme", "cai", "scratch")
    assert service.delete_skill("acme", "ben", "scratch") == {"deleted": "scratch"}
    with pytest.raises(NotFound):
        service.get_skill("acme", "cai", "scratch")


def test_ownership_transfer_and_member_removal(service, team, published):
    with pytest.raises(Invalid):
        service.set_owner("acme", "ben", published, "dee")  # viewers cannot own
    service.set_owner("acme", "ben", published, "ben")
    with pytest.raises(Forbidden):
        service.update_draft("acme", "cai", published, skill_text())  # cai no longer owns it
    service.set_owner("acme", "ana", published, "cai")
    result = service.remove_member("acme", "ana", "cai")
    assert result["reassigned_skills"] == [published]
    assert service.get_skill("acme", "ana", published)["owner"] == "ana"


def test_list_filters_and_dashboard(service, team, published):
    service.create_skill("acme", "cai", skill_text(name="pr-review", description="Review pull requests the team's way."))
    service.run_checks("acme", "cai", "pr-review")
    service.submit_review("acme", "cai", "pr-review")
    service.create_skill("acme", "cai", skill_text(name="onboarding"))
    assert [s["slug"] for s in service.list_skills("acme", "dee")] == ["onboarding", "pr-review", "release-notes"]
    assert [s["slug"] for s in service.list_skills("acme", "dee", status="in_review")] == ["pr-review"]
    assert [s["slug"] for s in service.list_skills("acme", "dee", tag="docs")] == ["onboarding", "pr-review", "release-notes"]
    assert [s["slug"] for s in service.list_skills("acme", "dee", query="team's way")] == ["pr-review"]
    with pytest.raises(Invalid):
        service.list_skills("acme", "dee", status="weird")
    service.record_install("acme", "dee", published, "1.0.0", "claude-code", "/tmp/x/SKILL.md", "abc")
    d = service.dashboard("acme", "dee")
    assert d["by_status"] == {"draft": 1, "in_review": 1, "approved": 0, "published": 1, "deprecated": 0}
    assert d["most_installed"][0] == {"slug": published, "title": "Release Notes", "installs": 1}
    assert [r["skill"] for r in d["pending_reviews"]] == ["pr-review"]
    assert d["drafts_in_progress"][0]["slug"] == "onboarding"
    actions = [a["action"] for a in service.activity("acme", "dee", limit=3)]
    assert actions == ["skill.installed", "skill.created", "review.submitted"]


def test_install_records_and_visibility(service, team, published):
    with pytest.raises(NotFound):
        service.record_install("acme", "dee", published, "3.0.0", "claude-code", "/p", "h")
    service.record_install("acme", "dee", published, "1.0.0", "claude-code", "/p", "h")
    service.record_install("acme", "cai", published, "1.0.0", "codex", "/q", "h")
    assert [i["target"] for i in service.list_installs("acme", "dee")] == ["claude-code"]
    with pytest.raises(Forbidden):
        service.list_installs("acme", "dee", handle="cai")
    assert len(service.list_installs("acme", "ben", handle="*")) == 2
    assert service.remove_install("acme", "dee", published, "claude-code")["removed"] is True
    assert service.remove_install("acme", "dee", published, "claude-code")["removed"] is False


def test_catalog_index_is_deterministic_and_changes_on_publish(service, team, published):
    service.create_skill("acme", "cai", skill_text(name="unpublished"))
    a = service.catalog_index("acme", "dee")
    b = service.catalog_index("acme", "dee")
    assert [e["slug"] for e in a["skills"]] == [published] and a["index_hash"] == b["index_hash"]
    assert a["generation"] == b["generation"] and a["generation"].endswith(a["index_hash"][:12])
    content = service.get_content("acme", "dee", published)
    assert a["skills"][0]["content_hash"] == content["content_hash"] == skillfile.content_hash(content["content"])
    assert a["skills"][0]["channel"] == "production"
    service.update_draft("acme", "cai", published, skill_text(body_extra="\n## Changed\n\nx.\n"))
    publish(service, published, release=None)
    b2 = service.catalog_index("acme", "dee")
    assert b2["index_hash"] == a["index_hash"]  # approved but unreleased versions do not change the index
    service.release("acme", "ben", published, channel="canary")
    c = service.catalog_index("acme", "dee")
    assert c["index_hash"] != a["index_hash"]
    assert {(e["channel"], e["version"]) for e in c["skills"]} == {("production", "1.0.0"), ("canary", "1.1.0")}
