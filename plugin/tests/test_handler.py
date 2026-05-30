"""Phase 6 gate: full handler pipeline (vision mocked + HTTP mocked) + approval hook.

Covers: happy create, dedup short-circuit (no create), preview (no create),
missing credentials (no network), invalid image (no network/LLM), missing config,
and the pre_tool_call approval gate's allow/deny decisions.
"""

from __future__ import annotations

import json

import pytest

import hermes_plugins.hermes_bug_vision_ticket as pkg
from hermes_plugins.hermes_bug_vision_ticket import clients, vision

CONFIG = """\
default_target: github_issues
require_approval: true
targets:
  github_issues:
    repo: acme/web
    default_labels: [bug, from-screenshot]
    severity_map:
      blocker: { labels: [severity:blocker] }
      critical: { labels: [severity:critical] }
      major: { labels: [severity:major] }
      minor: { labels: [severity:minor] }
      trivial: { labels: [severity:trivial] }
    dedup:
      enabled: true
      search_template: 'repo:{repo} is:issue is:open in:title {title}'
"""

REPORT = {
    "title": "Save button overlaps footer",
    "summary": "The Save button renders on top of the footer text.",
    "steps_to_reproduce": ["Open settings", "Scroll down"],
    "expected_behavior": "Save sits above the footer.",
    "actual_behavior": "Save overlaps the footer.",
    "severity": "critical",
    "component_hint": "settings",
    "ui_elements_observed": ["Save button"],
    "visible_text": ["Save"],
    "confidence": "high",
}


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.text = ""

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(clients.time, "sleep", lambda *a, **k: None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a github config + GITHUB_TOKEN + a real png."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_token")
    (home / "bug-tickets.yaml").write_text(CONFIG, encoding="utf-8")
    img = tmp_path / "bug.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"pixels")
    # Vision is mocked — no real LLM call.
    monkeypatch.setattr(vision, "extract_bug_report", lambda ctx, path, **kw: dict(REPORT))
    return {"home": home, "img": str(img)}


def _route(monkeypatch, *, dup=False, created_number=42):
    """Mock GitHub search (dedup) + create endpoints; record calls."""
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url))
        if "/search/issues" in url:
            # Real GitHub search results carry the title; dedup verifies it matches.
            items = (
                [{"html_url": "https://github.com/acme/web/issues/3", "title": REPORT["title"]}]
                if dup else []
            )
            return FakeResp(200, {"items": items})
        if url.endswith("/issues"):
            return FakeResp(201, {"html_url": f"https://github.com/acme/web/issues/{created_number}", "number": created_number})
        # Record (don't raise) unexpected calls: an AssertionError here would be
        # swallowed by _run_pipeline's broad except and surface as internal_error,
        # hiding which endpoint was hit. Tests assert `not _unexpected(calls)` instead.
        calls.append(("UNEXPECTED", url))
        return FakeResp(404, {"message": f"unexpected {method} {url}"})

    monkeypatch.setattr(clients.requests, "request", fake)
    return calls


def _unexpected(calls):
    return [u for m, u in calls if m == "UNEXPECTED"]


def _run(env, **args):
    args.setdefault("image_path", env["img"])
    return json.loads(pkg._run_pipeline(object(), args))


# --- pipeline ---------------------------------------------------------------
def test_happy_create(env, monkeypatch):
    calls = _route(monkeypatch, created_number=42)
    out = _run(env, confirm=True)
    assert out["success"] is True and out["created"] is True
    assert out["ticket_url"] == "https://github.com/acme/web/issues/42"
    assert out["title"] == REPORT["title"]
    # dedup search then create POST.
    assert any("/search/issues" in u for _m, u in calls)
    assert any(u.endswith("/issues") and m == "POST" for m, u in calls)
    assert not _unexpected(calls)  # no endpoint other than search + create was hit


def test_dedup_short_circuits_create(env, monkeypatch):
    calls = _route(monkeypatch, dup=True)
    out = _run(env, confirm=True)
    assert out["success"] is True and out.get("deduped") is True
    assert out["ticket_url"] == "https://github.com/acme/web/issues/3"
    # No create POST happened.
    assert not any(m == "POST" and u.endswith("/issues") for m, u in calls)


def test_preview_does_not_create(env, monkeypatch):
    calls = _route(monkeypatch, dup=False)
    out = _run(env, confirm=False)
    assert out["success"] is True and out["preview"] is True
    assert out["requires_confirmation"] is True
    assert out["title"] == REPORT["title"] and out["severity"] == "critical"
    assert not any(m == "POST" for m, u in calls)  # nothing created


def test_missing_credentials_no_network(env, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    calls = _route(monkeypatch)
    out = _run(env, confirm=True)
    assert out["success"] is False and out["error"] == "missing_credentials"
    assert calls == []  # never hit the network


def test_invalid_image_no_llm_no_network(env, monkeypatch):
    calls = _route(monkeypatch)
    # Make extract blow up if reached — it must not be.
    monkeypatch.setattr(vision, "extract_bug_report", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called")))
    out = _run(env, confirm=True, image_path="/tmp/nope-xyz.png")
    assert out["success"] is False and out["error"] == "image_not_found"
    assert calls == []


def test_missing_config(env, monkeypatch):
    (env["home"] / "bug-tickets.yaml").unlink()
    out = _run(env, confirm=True)
    assert out["success"] is False and out["error"] == "config_missing"


def test_target_override(env, monkeypatch):
    # Only github is configured; selecting jira must error clearly.
    out = _run(env, confirm=True, target="jira")
    assert out["success"] is False and out["error"] == "target_not_configured"


def test_internal_error_catch_all(env, monkeypatch):
    # An unexpected (non-BugTicketError) failure must become a structured
    # internal_error JSON string, never crash the agent loop.
    from hermes_plugins.hermes_bug_vision_ticket import config as cfg_mod

    def boom():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cfg_mod, "load_config", boom)
    out = _run(env, confirm=True)
    assert out["success"] is False and out["error"] == "internal_error"


JIRA_CONFIG = """\
default_target: jira
targets:
  jira:
    base_url: https://acme.atlassian.net
    project_key: ENG
    issue_type: Bug
    severity_map:
      blocker:  { priority: { name: Highest } }
      critical: { priority: { name: High } }
      major:    { priority: { name: Medium } }
      minor:    { priority: { name: Low } }
      trivial:  { priority: { name: Lowest } }
    dedup:
      enabled: true
      jql_template: 'project = {project_key} AND summary ~ "{title}"'
"""

LINEAR_CONFIG = """\
default_target: linear
targets:
  linear:
    team_id: team-1
    severity_map:
      blocker:  { priority: 1 }
      critical: { priority: 1 }
      major:    { priority: 2 }
      minor:    { priority: 3 }
      trivial:  { priority: 4 }
    dedup: { enabled: true }
"""


def test_jira_end_to_end(env, monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "bot@acme.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "jt")
    (env["home"] / "bug-tickets.yaml").write_text(JIRA_CONFIG, encoding="utf-8")

    seen = []

    def fake(method, url, **kw):
        seen.append((method, url))
        if url.endswith("/rest/api/3/search/jql"):
            return FakeResp(200, {"issues": []})
        if url.endswith("/rest/api/3/issue"):
            return FakeResp(201, {"key": "ENG-5", "id": "5000"})
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(clients.requests, "request", fake)
    out = _run(env, confirm=True)
    assert out["created"] is True
    assert out["ticket_url"] == "https://acme.atlassian.net/browse/ENG-5"
    assert any(u.endswith("/rest/api/3/search/jql") for _m, u in seen)  # dedup ran


def test_linear_end_to_end_dedup_short_circuits(env, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lk")
    (env["home"] / "bug-tickets.yaml").write_text(LINEAR_CONFIG, encoding="utf-8")

    seen = []

    def fake(method, url, **kw):
        seen.append((method, url, kw["json"]["query"]))
        query = kw["json"]["query"]
        if "issueSearch" in query:  # dedup: return a title-matching node
            return FakeResp(200, {"data": {"issueSearch": {"nodes": [
                {"id": "1", "url": "https://linear.app/acme/issue/ENG-1", "title": REPORT["title"]}
            ]}}})
        raise AssertionError("issueCreate must not be called when a duplicate exists")

    monkeypatch.setattr(clients.requests, "request", fake)
    out = _run(env, confirm=True)
    assert out.get("deduped") is True
    assert out["ticket_url"] == "https://linear.app/acme/issue/ENG-1"
    assert all("issueCreate" not in q for *_x, q in seen)  # never created


# --- approval hook ----------------------------------------------------------
def test_hook_ignores_other_tools():
    assert pkg._on_pre_tool_call(tool_name="terminal", args={}) is None


def test_hook_allows_confirmed():
    assert pkg._on_pre_tool_call(tool_name=pkg.TOOL_NAME, args={"confirm": True}) is None


def test_hook_blocks_unconfirmed_when_required(env):
    res = pkg._on_pre_tool_call(tool_name=pkg.TOOL_NAME, args={"target": "github_issues"})
    assert res is not None and res["action"] == "block"
    assert "github_issues" in res["message"]
    assert "confirm=true" in res["message"]


def test_hook_allows_when_approval_disabled(env):
    (env["home"] / "bug-tickets.yaml").write_text(
        CONFIG.replace("require_approval: true", "require_approval: false"), encoding="utf-8"
    )
    assert pkg._on_pre_tool_call(tool_name=pkg.TOOL_NAME, args={}) is None


def test_hook_blocks_when_no_config(tmp_path, monkeypatch):
    # No config -> default to requiring approval (safe).
    home = tmp_path / "empty_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    res = pkg._on_pre_tool_call(tool_name=pkg.TOOL_NAME, args={})
    assert res is not None and res["action"] == "block"


def test_hook_accepts_full_host_kwargs(env):
    # The host invokes the hook with task_id/session_id/tool_call_id too; the hook's
    # **_kwargs must absorb them (a narrowed signature would TypeError -> host
    # swallows it -> fail OPEN, allowing unconfirmed creation).
    extra = {"task_id": "t1", "session_id": "s1", "tool_call_id": "c1"}
    blocked = pkg._on_pre_tool_call(tool_name=pkg.TOOL_NAME, args={"target": "github_issues"}, **extra)
    assert blocked is not None and blocked["action"] == "block"
    allowed = pkg._on_pre_tool_call(tool_name=pkg.TOOL_NAME, args={"confirm": True}, **extra)
    assert allowed is None


def test_hook_blocks_on_invalid_config(env):
    # A present-but-invalid config must fail safe (block), like a missing one.
    (env["home"] / "bug-tickets.yaml").write_text("default_target: jira\n", encoding="utf-8")  # no targets
    res = pkg._on_pre_tool_call(tool_name=pkg.TOOL_NAME, args={})
    assert res is not None and res["action"] == "block"


# --- F1: strict confirm parsing (the gate must not fail open on a non-boolean) ---
@pytest.mark.parametrize("falsey", ["false", "False", "no", "0", "", "off", 0, False, None])
def test_confirmed_rejects_non_affirmative(falsey):
    # The host does NOT coerce tool args to the schema's types, so a stringified
    # "false" would be truthy under a naive bool(); _confirmed must treat every
    # non-affirmative value as NOT confirmed.
    assert pkg._confirmed({"confirm": falsey}) is False


@pytest.mark.parametrize("truthy", ["true", "True", "yes", "1", True, 1])
def test_confirmed_accepts_affirmative(truthy):
    assert pkg._confirmed({"confirm": truthy}) is True


def test_string_false_confirm_does_not_create(env, monkeypatch):
    # confirm="false" (a truthy string) must NOT create a ticket — it must preview.
    calls = _route(monkeypatch, created_number=42)
    out = _run(env, confirm="false")
    assert out["success"] is True and out.get("preview") is True
    assert not any(m == "POST" for m, u in calls)  # nothing created
    assert not _unexpected(calls)


def test_hook_blocks_string_false_confirm(env):
    # The approval hook must also block confirm="false" (regression for the
    # truthy-string fail-open).
    res = pkg._on_pre_tool_call(tool_name=pkg.TOOL_NAME, args={"confirm": "false", "target": "github_issues"})
    assert res is not None and res["action"] == "block"


def test_string_true_confirm_creates(env, monkeypatch):
    # A stringified "true" is a valid affirmative and should create.
    calls = _route(monkeypatch, created_number=77)
    out = _run(env, confirm="true")
    assert out["success"] is True and out.get("created") is True
    assert any(m == "POST" and u.endswith("/issues") for m, u in calls)
