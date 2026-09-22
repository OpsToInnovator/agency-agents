import pytest

from skillcurrent.errors import Conflict, Forbidden, Invalid, NotFound
from tests.conftest import publish, skill_text


def test_release_rollback_and_history(service, team, published):
    hist = service.release_history("acme", "dee", published)
    assert hist["channels"]["production"]["version"] == "1.0.0"
    assert [e["kind"] for e in hist["events"]] == ["release"]

    with pytest.raises(Conflict, match="already the production release"):
        service.release("acme", "ben", published)
    with pytest.raises(NotFound):
        service.release("acme", "ben", published, "9.9.9")
    with pytest.raises(Invalid):
        service.release("acme", "ben", published, channel="beta")
    with pytest.raises(Forbidden):
        service.release("acme", "cai", published)
    with pytest.raises(Invalid, match="no earlier production release"):
        service.rollback("acme", "ben", published)

    service.update_draft("acme", "cai", published, skill_text(body_extra="\n## Two\n\nx.\n"))
    publish(service, published, release=None, note="second")
    view = service.get_skill("acme", "dee", published)
    assert view["latest_version"] == "1.1.0" and view["production_version"] == "1.0.0" and view["status"] == "published"

    canary = service.release("acme", "ben", published, channel="canary", note="try it on two machines")
    assert canary["version"] == "1.1.0" and canary["previous"] is None
    assert service.get_content("acme", "dee", published, ref="canary")["version"] == "1.1.0"
    assert service.get_content("acme", "dee", published)["version"] == "1.0.0"

    prod = service.release("acme", "ana", published, "1.1.0")
    assert prod["previous"] == "1.0.0"
    back = service.rollback("acme", "ana", published, reason="handoff regression")
    assert back["version"] == "1.0.0" and back["previous"] == "1.1.0"
    assert service.get_content("acme", "dee", published)["version"] == "1.0.0"
    hist = service.release_history("acme", "dee", published)
    assert [(e["kind"], e["channel"], e["version"]) for e in hist["events"]] == [
        ("rollback", "production", "1.0.0"), ("release", "production", "1.1.0"), ("release", "canary", "1.1.0"), ("release", "production", "1.0.0"),
    ]
    assert hist["events"][0]["reason"] == "handoff regression"
    # Rolling back again returns to 1.1.0 (the version served before the rollback).
    assert service.rollback("acme", "ana", published)["version"] == "1.1.0"

    service.deprecate("acme", "ben", published, "retired")
    with pytest.raises(Conflict):
        service.release("acme", "ben", published, "1.0.0")


def test_approve_can_release_directly_and_status_reflects_channels(service, team):
    service.create_skill("acme", "cai", skill_text(name="onboarding"))
    with pytest.raises(Invalid):
        publish(service, "onboarding", release="staging")
    # the failed approve above did not consume the pending review
    out = service.approve("acme", "ben", "onboarding", release="canary")
    assert out["status"] == "approved" and out["channels"]["canary"]["version"] == "1.0.0"
    assert service.list_skills("acme", "dee", status="approved")[0]["slug"] == "onboarding"
    service.release("acme", "ben", "onboarding")
    assert service.list_skills("acme", "dee", status="published")[0]["slug"] == "onboarding"


def test_rules_are_stored_and_enforced(service, team):
    with pytest.raises(Forbidden):
        service.add_rule("acme", "cai", "x", "forbid", "y")
    with pytest.raises(Invalid):
        service.add_rule("acme", "ben", "Bad Name", "forbid", "y")
    with pytest.raises(Invalid):
        service.add_rule("acme", "ben", "bad-kind", "must", "y")
    with pytest.raises(Invalid):
        service.add_rule("acme", "ben", "bad-regex", "forbid", "(")
    rule = service.add_rule("acme", "ben", "inspect-first", "require", r"inspect .* before", category="evidence", rationale="Inspect what the agent can already see.")
    assert rule["created_by"] == "ben"
    with pytest.raises(Conflict):
        service.add_rule("acme", "ben", "inspect-first", "require", "x")
    service.create_skill("acme", "cai", skill_text(name="handoff"))
    report = service.run_checks("acme", "cai", "handoff")
    assert not report["passed"] and any(r["id"] == "rule:inspect-first" and not r["passed"] for r in report["results"])
    with pytest.raises(Conflict, match="fails 1 check"):
        service.submit_review("acme", "cai", "handoff")
    service.update_draft("acme", "cai", "handoff", skill_text(name="handoff", body_extra="\n4. Inspect connectors before asking.\n"))
    assert service.run_checks("acme", "cai", "handoff")["passed"]
    service.submit_review("acme", "cai", "handoff")
    assert service.remove_rule("acme", "ben", "inspect-first") == {"removed": "inspect-first"}
    with pytest.raises(NotFound):
        service.remove_rule("acme", "ben", "inspect-first")
    assert service.check_content("acme", "dee", skill_text())["passed"]


def test_passport(service, team, published):
    p = service.passport("acme", "dee", published)
    assert p["canonical_id"] == f"acme/{published}" and p["channels"]["production"]["version"] == "1.0.0"
    assert p["environments"]["environments"] == 0 and p["rules"] == []
