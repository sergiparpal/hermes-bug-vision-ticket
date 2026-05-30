"""Phase 4 gate (mapping): pure BugReport -> tracker payload mapping.

Covers per-tracker payload shape, severity mapping, {placeholder} expansion,
body rendering, dedup query building, and the failure modes: missing custom-field
placeholder, unmapped severity, unknown target, missing project.
"""

from __future__ import annotations

import re

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


def test_jira_dedup_escapes_backslash_and_all_interpolated_fields():
    # Untrusted title with a backslash + quote, and a template that also
    # interpolates {summary} — every field must be escaped, backslash first.
    bug = dict(BUG, title='path\\to "x"', summary='he "said"')
    cfg = dict(JIRA_CFG, dedup={
        "enabled": True,
        "jql_template": 'summary ~ "{title}" OR description ~ "{summary}"',
    })
    d = mapping.build_dedup(bug, "jira", cfg, "ENG")
    assert "\\\\" in d["jql"]            # the single backslash was doubled
    assert 'he \\"said\\"' in d["jql"]   # quotes in {summary} were escaped too


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


def test_invalid_severity_map_scalar_entry():
    cfg = dict(JIRA_CFG, severity_map={"critical": "High"})  # scalar, not a mapping
    with pytest.raises(BugTicketError) as ei:
        mapping.to_payload(BUG, "jira", cfg)
    assert ei.value.error == "invalid_severity_map"


def test_missing_title():
    bug = {k: v for k, v in BUG.items() if k != "title"}
    with pytest.raises(BugTicketError) as ei:
        mapping.to_payload(bug, "jira", JIRA_CFG)
    assert ei.value.error == "missing_title"


def test_dedup_disabled_returns_none():
    cfg = dict(JIRA_CFG, dedup={"enabled": False})
    assert mapping.build_dedup(BUG, "jira", cfg, "ENG") is None
    cfg2 = {k: v for k, v in JIRA_CFG.items() if k != "dedup"}
    assert mapping.build_dedup(BUG, "jira", cfg2, "ENG") is None


# --- project/repo validation ------------------------------------------------
@pytest.mark.parametrize("bad", ["ENG ORDER BY created", "ENG OR x", 'a"b', "1ENG", "ENG-2"])
def test_jira_invalid_project_key(bad):
    with pytest.raises(BugTicketError) as ei:
        mapping.to_payload(BUG, "jira", JIRA_CFG, project=bad)
    assert ei.value.error == "invalid_project_key"


@pytest.mark.parametrize("bad", ["owner/", "/name", "a/b/c", "acme/web?x=1", "..", "owner/ b"])
def test_github_repo_malformed_rejected(bad):
    with pytest.raises(BugTicketError) as ei:
        mapping.to_payload(BUG, "github_issues", dict(GITHUB_CFG, repo=bad))
    assert ei.value.error == "invalid_repo"


def test_github_repo_valid_dotted_name_accepted():
    out = mapping.to_payload(BUG, "github_issues", dict(GITHUB_CFG, repo="acme/web.js"))
    assert out["project"] == "acme/web.js"


# --- reserved-field guard (severity_map / custom_fields cannot clobber core) -
def test_jira_severity_map_reserved_key_rejected():
    cfg = dict(JIRA_CFG, severity_map={"critical": {"summary": "PWNED", "priority": {"name": "High"}}})
    with pytest.raises(BugTicketError) as ei:
        mapping.to_payload(BUG, "jira", cfg)
    assert ei.value.error == "reserved_field_override"


def test_jira_custom_fields_reserved_key_rejected():
    cfg = dict(JIRA_CFG, custom_fields={"description": "clobbered"})
    with pytest.raises(BugTicketError) as ei:
        mapping.to_payload(BUG, "jira", cfg)
    assert ei.value.error == "reserved_field_override"


def test_linear_severity_map_reserved_key_rejected():
    cfg = dict(LINEAR_CFG, severity_map={"critical": {"title": "PWNED", "priority": 1}})
    with pytest.raises(BugTicketError) as ei:
        mapping.to_payload(BUG, "linear", cfg)
    assert ei.value.error == "reserved_field_override"


# --- GitHub dedup qualifier-injection sanitization --------------------------
def test_github_dedup_sanitizes_qualifier_injection():
    bug = dict(BUG, title="Save fails repo:victim/secret is:open")
    d = mapping.build_dedup(bug, "github_issues", GITHUB_CFG, "acme/web")
    # The structural repo: qualifier (from the template) stays; the injected ':'
    # tokens from the untrusted title are neutralized, so no second repo:/is: lands.
    assert d["q"].startswith("repo:acme/web is:issue is:open in:title ")
    injected = d["q"].split("in:title ", 1)[1]
    assert ":" not in injected  # qualifier delimiter stripped from untrusted text
    assert d["title"] == bug["title"]  # original title kept for client-side verification


# --- Linear label_ids: no fallback to display-name `labels` -----------------
def test_linear_labels_key_is_not_used_as_label_ids():
    cfg = {k: v for k, v in LINEAR_CFG.items() if k != "label_ids"}
    cfg["labels"] = ["bug", "ui"]  # display names — must NOT become labelIds
    payload = mapping.to_payload(BUG, "linear", cfg)["create_payload"]
    assert "labelIds" not in payload


# --- extracted observed fields are rendered into the body -------------------
def test_observed_fields_rendered_into_body():
    body = mapping.to_payload(BUG, "github_issues", GITHUB_CFG)["create_payload"]["body"]
    assert "Affected component: settings-page" in body
    assert "## Observed UI elements" in body and "Save button" in body
    assert "Text observed in screenshot" in body and "Save" in body


# --- F3: JQL control-char stripping (defense-in-depth) ----------------------
def test_jira_dedup_strips_control_chars():
    # Newlines / control chars have no place in a one-line JQL literal and are a
    # multiline-breakout aid; they must be stripped (each -> one space).
    bug = dict(BUG, title="a\x00b\nc\td")
    d = mapping.build_dedup(bug, "jira", JIRA_CFG, "ENG")
    assert "\n" not in d["jql"] and "\t" not in d["jql"] and "\x00" not in d["jql"]
    assert 'summary ~ "a b c d"' in d["jql"]


# --- F4: untrusted body text is Markdown-escaped (GitHub/Linear), ADF is not -
def test_github_body_defangs_markdown_beacon():
    # A crafted screenshot whose OCR'd text is an image beacon / link must render
    # as inert text in the issue body (the '[', ']', '!' are escaped).
    bug = dict(BUG, visible_text=["![x](http://attacker/leak.png)", "[click](http://evil)"])
    body = mapping.to_payload(bug, "github_issues", GITHUB_CFG)["create_payload"]["body"]
    assert "![x](http://attacker/leak.png)" not in body  # raw beacon not present
    assert "\\!\\[x\\](http://attacker/leak.png)" in body  # escaped form present
    assert "\\[click\\](http://evil)" in body


def test_linear_body_defangs_inline_html_and_code():
    bug = dict(BUG, summary="<img src=x onerror=1> and `code`")
    body = mapping.to_payload(bug, "linear", LINEAR_CFG)["create_payload"]["description"]
    # The escaped forms are present (so the HTML/code render inert)...
    assert "\\<img src=x onerror=1\\>" in body
    assert "\\`code\\`" in body
    # ...and no unescaped '<' or '`' survives (every one is backslash-prefixed).
    assert not re.search(r"(?<!\\)<", body)
    assert not re.search(r"(?<!\\)`", body)


def test_jira_adf_text_is_not_escaped():
    # The ADF path places text in inert text nodes, so it must be the RAW string
    # (escaping there would inject literal backslashes into Jira).
    bug = dict(BUG, summary="brackets [x] and <html> stay raw")
    adf = mapping.to_payload(bug, "jira", JIRA_CFG)["create_payload"]["fields"]["description"]
    texts = _adf_texts(adf)
    assert "brackets [x] and <html> stay raw" in texts
    assert not any("\\[" in t or "\\<" in t for t in texts)


def _adf_texts(node):
    """Collect every ADF text-node string (recursively)."""
    out = []
    if isinstance(node, dict):
        if node.get("type") == "text":
            out.append(node.get("text", ""))
        for child in node.get("content", []) or []:
            out.extend(_adf_texts(child))
    return out
