"""Phase 4 gate (mapping): pure BugReport -> tracker payload mapping.

Covers per-tracker payload shape, severity mapping, {placeholder} expansion,
body rendering, dedup query building, and the failure modes: missing custom-field
placeholder, unmapped severity, unknown target, missing project.
"""

from __future__ import annotations

import pytest

from hermes_plugins.hermes_bug_vision_ticket import mapping
from hermes_plugins.hermes_bug_vision_ticket.errors import BugTicketError

BUG = {
    "title": "Save button overlaps footer",
    "summary": "The Save button renders on top of the footer.",
    "steps_to_reproduce": ["Open settings", "Scroll to bottom"],
    "expected_behavior": "Save sits above the footer.",
    "actual_behavior": "Save overlaps the footer text.",
    "severity": "critical",
    "component_hint": "settings-page",
    "ui_elements_observed": ["Save button"],
    "visible_text": ["Save"],
    "confidence": "high",
}

JIRA_CFG = {
    "project_key": "ENG",
    "issue_type": "Bug",
    "labels": ["from-screenshot"],
    "severity_map": {
        "blocker": {"priority": {"name": "Highest"}},
        "critical": {"priority": {"name": "High"}},
        "major": {"priority": {"name": "Medium"}},
        "minor": {"priority": {"name": "Low"}},
        "trivial": {"priority": {"name": "Lowest"}},
    },
    "custom_fields": {"customfield_10010": "{component_hint}"},
    "dedup": {
        "enabled": True,
        "jql_template": 'project = {project_key} AND summary ~ "{title}" AND statusCategory != Done',
    },
}

LINEAR_CFG = {
    "team_id": "team-uuid-123",
    "severity_map": {
        "blocker": {"priority": 1},
        "critical": {"priority": 1},
        "major": {"priority": 2},
        "minor": {"priority": 3},
        "trivial": {"priority": 4},
    },
    "label_ids": ["label-uuid-ui"],
    "dedup": {"enabled": True},
}

GITHUB_CFG = {
    "repo": "acme/web",
    "default_labels": ["bug", "from-screenshot"],
    "severity_map": {
        "blocker": {"labels": ["severity:blocker"]},
        "critical": {"labels": ["severity:critical"]},
        "major": {"labels": ["severity:major"]},
        "minor": {"labels": ["severity:minor"]},
        "trivial": {"labels": ["severity:trivial"]},
    },
    "dedup": {
        "enabled": True,
        "search_template": "repo:{repo} is:issue is:open in:title {title}",
    },
}


# --- Jira -------------------------------------------------------------------
def test_jira_payload():
    out = mapping.to_payload(BUG, "jira", JIRA_CFG)
    assert out["target"] == "jira"
    assert out["project"] == "ENG"
    fields = out["create_payload"]["fields"]
    assert fields["project"] == {"key": "ENG"}
    assert fields["issuetype"] == {"name": "Bug"}
    assert fields["summary"] == BUG["title"]
    assert fields["priority"] == {"name": "High"}  # critical -> High
    assert fields["labels"] == ["from-screenshot"]
    assert fields["customfield_10010"] == "settings-page"  # placeholder expanded
    # Description is an ADF doc.
    desc = fields["description"]
    assert desc["type"] == "doc" and desc["version"] == 1
    headings = [c["content"][0]["text"] for c in desc["content"] if c["type"] == "heading"]
    assert "Steps to reproduce" in headings


def test_jira_summary_truncated_to_255():
    bug = dict(BUG, title="x" * 400)
    fields = mapping.to_payload(bug, "jira", JIRA_CFG)["create_payload"]["fields"]
    assert len(fields["summary"]) == 255


def test_jira_project_override():
    out = mapping.to_payload(BUG, "jira", JIRA_CFG, project="OPS")
    assert out["project"] == "OPS"
    assert out["create_payload"]["fields"]["project"] == {"key": "OPS"}


def test_jira_dedup_jql_escapes_quotes():
    bug = dict(BUG, title='He said "hi"')
    d = mapping.build_dedup(bug, "jira", JIRA_CFG, "ENG")
    assert d["kind"] == "jql"
    assert 'project = ENG' in d["jql"]
    assert '\\"hi\\"' in d["jql"]  # quotes escaped for JQL


# --- Linear -----------------------------------------------------------------
def test_linear_payload():
    out = mapping.to_payload(BUG, "linear", LINEAR_CFG)
    p = out["create_payload"]
    assert p["teamId"] == "team-uuid-123"
    assert p["title"] == BUG["title"]
    assert p["priority"] == 1  # critical -> 1
    assert p["labelIds"] == ["label-uuid-ui"]
    assert p["description"].startswith(BUG["summary"])  # markdown body
    assert "## Steps to reproduce" in p["description"]


def test_linear_dedup_by_title():
    d = mapping.build_dedup(BUG, "linear", LINEAR_CFG, "team-uuid-123")
    assert d == {"kind": "linear", "title": BUG["title"]}


# --- GitHub -----------------------------------------------------------------
def test_github_payload():
    out = mapping.to_payload(BUG, "github_issues", GITHUB_CFG)
    assert out["project"] == "acme/web"
    p = out["create_payload"]
    assert p["title"] == BUG["title"]
    assert p["labels"] == ["bug", "from-screenshot", "severity:critical"]
    assert "## Actual behavior" in p["body"]


def test_github_invalid_repo():
    with pytest.raises(BugTicketError) as ei:
        mapping.to_payload(BUG, "github_issues", dict(GITHUB_CFG, repo="noslug"))
    assert ei.value.error == "invalid_repo"


def test_github_dedup_search():
    d = mapping.build_dedup(BUG, "github_issues", GITHUB_CFG, "acme/web")
    assert d["kind"] == "github_search"
    assert d["q"] == "repo:acme/web is:issue is:open in:title Save button overlaps footer"


# --- Failure modes ----------------------------------------------------------
def test_unmapped_severity():
    bug = dict(BUG, severity="major")
    cfg = dict(JIRA_CFG, severity_map={"critical": {"priority": {"name": "High"}}})
    with pytest.raises(BugTicketError) as ei:
        mapping.to_payload(bug, "jira", cfg)
    assert ei.value.error == "unmapped_severity"


def test_severity_map_missing():
    cfg = {k: v for k, v in JIRA_CFG.items() if k != "severity_map"}
    with pytest.raises(BugTicketError) as ei:
        mapping.to_payload(BUG, "jira", cfg)
    assert ei.value.error == "severity_map_missing"


def test_missing_custom_field_placeholder():
    cfg = dict(JIRA_CFG, custom_fields={"customfield_999": "{does_not_exist}"})
    with pytest.raises(BugTicketError) as ei:
        mapping.to_payload(BUG, "jira", cfg)
    assert ei.value.error == "missing_placeholder"


def test_unknown_target():
    with pytest.raises(BugTicketError) as ei:
        mapping.to_payload(BUG, "trello", {})
    assert ei.value.error == "unknown_target"


def test_missing_project():
    cfg = {k: v for k, v in JIRA_CFG.items() if k != "project_key"}
    with pytest.raises(BugTicketError) as ei:
        mapping.to_payload(BUG, "jira", cfg)
    assert ei.value.error == "missing_project"


def test_dedup_disabled_returns_none():
    cfg = dict(JIRA_CFG, dedup={"enabled": False})
    assert mapping.build_dedup(BUG, "jira", cfg, "ENG") is None
    cfg2 = {k: v for k, v in JIRA_CFG.items() if k != "dedup"}
    assert mapping.build_dedup(BUG, "jira", cfg2, "ENG") is None
