"""Phase 5 gate: REST clients with mocked HTTP (no network, no real tokens).

Covers create success, dedup hit/miss, 401 -> invalid_credentials, 404 ->
not_found, timeout -> tracker_timeout, 5xx -> bounded retry then success,
HTTPS+timeout enforcement, and missing-credential / insecure-URL handling.
"""

from __future__ import annotations

import pytest
import requests

from hermes_plugins.hermes_bug_vision_ticket import clients
from hermes_plugins.hermes_bug_vision_ticket.errors import BugTicketError


class FakeResp:
    def __init__(self, status=200, payload=None, text="", headers=None):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.text = text
        self.headers = {} if headers is None else headers

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(clients.time, "sleep", lambda *a, **k: None)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@acme.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-token")
    monkeypatch.setenv("LINEAR_API_KEY", "lin_key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_token")


def patch_request(monkeypatch, handler):
    """Patch requests.request with a handler(method, url, **kwargs) -> FakeResp."""
    calls = []

    def fake(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return handler(method, url, calls, **kwargs)

    monkeypatch.setattr(clients.requests, "request", fake)
    return calls


# --- GitHub -----------------------------------------------------------------
def test_github_create_success(creds, monkeypatch):
    def handler(method, url, calls, **kw):
        assert url.startswith("https://")          # HTTPS enforced
        assert kw["timeout"] == clients.DEFAULT_TIMEOUT  # explicit timeout
        assert kw["headers"]["Authorization"] == "Bearer ghp_token"
        return FakeResp(201, {"html_url": "https://github.com/acme/web/issues/7", "number": 7})

    patch_request(monkeypatch, handler)
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    out = client.create_issue("acme/web", {"title": "t", "body": "b"})
    assert out["url"] == "https://github.com/acme/web/issues/7"
    assert out["id"] == "7"


def test_github_dedup_hit_and_miss(creds, monkeypatch):
    def hit(method, url, calls, **kw):
        assert method == "GET"                       # correct verb
        assert url.endswith("/search/issues")        # correct endpoint
        assert kw["params"]["q"] == "in:title bug"   # query forwarded
        return FakeResp(200, {"items": [{"html_url": "https://github.com/acme/web/issues/3"}]})

    patch_request(monkeypatch, hit)
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    assert client.find_duplicate({"kind": "github_search", "q": "in:title bug"}) == "https://github.com/acme/web/issues/3"

    patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(200, {"items": []}))
    assert client.find_duplicate({"kind": "github_search", "q": "x"}) is None


def test_github_non_json_2xx_is_tracker_error(creds, monkeypatch):
    import requests as _rq

    class BadJSON(FakeResp):
        def json(self):
            raise _rq.exceptions.JSONDecodeError("no json", "<html>", 0)

    patch_request(monkeypatch, lambda m, u, c, **k: BadJSON(200))
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("acme/web", {"title": "t"})
    assert ei.value.error == "tracker_error"


def test_github_connection_error_unreachable(creds, monkeypatch):
    def handler(method, url, calls, **kw):
        raise requests.exceptions.ConnectionError("no route")

    calls = patch_request(monkeypatch, handler)
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("acme/web", {"title": "t"})
    assert ei.value.error == "tracker_unreachable"
    assert len(calls) == clients._MAX_RETRIES + 1  # connection errors are retried, bounded


def test_github_401(creds, monkeypatch):
    patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(401, {"message": "Bad creds"}))
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("acme/web", {"title": "t"})
    assert ei.value.error == "invalid_credentials"
    assert "GITHUB_TOKEN" in ei.value.remediation


def test_github_404(creds, monkeypatch):
    patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(404, {"message": "Not Found"}))
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("acme/web", {"title": "t"})
    assert ei.value.error == "not_found"


def test_github_timeout(creds, monkeypatch):
    def handler(method, url, calls, **kw):
        raise requests.exceptions.Timeout("slow")

    calls = patch_request(monkeypatch, handler)
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("acme/web", {"title": "t"})
    assert ei.value.error == "tracker_timeout"
    assert len(calls) == clients._MAX_RETRIES + 1  # retried, bounded


def test_github_5xx_then_success(creds, monkeypatch):
    def handler(method, url, calls, **kw):
        if len(calls) == 1:
            return FakeResp(503, text="overloaded")
        return FakeResp(201, {"html_url": "https://github.com/acme/web/issues/9", "number": 9})

    calls = patch_request(monkeypatch, handler)
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    out = client.create_issue("acme/web", {"title": "t"})
    assert out["id"] == "9"
    assert len(calls) == 2  # one retry


def test_github_no_retry_on_4xx(creds, monkeypatch):
    calls = patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(422, {"message": "Validation"}))
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("acme/web", {"title": "t"})
    assert ei.value.error == "tracker_error"
    assert len(calls) == 1  # 4xx is not retried


def test_github_body_control_chars_scrubbed(creds, monkeypatch):
    # A tracker error body is echoed into the remediation; control chars must be
    # scrubbed so it can't smuggle escape/control sequences back to the agent.
    patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(422, text="bad\r\n\x1b[31mred\x00 input"))
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("acme/web", {"title": "t"})
    rem = ei.value.remediation
    assert "\x1b" not in rem and "\r" not in rem and "\x00" not in rem


def test_ssl_error_is_tls_error_not_retried(creds, monkeypatch):
    calls = patch_request(
        monkeypatch,
        lambda m, u, c, **k: (_ for _ in ()).throw(requests.exceptions.SSLError("cert verify failed")),
    )
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("acme/web", {"title": "t"})
    assert ei.value.error == "tls_error"
    assert len(calls) == 1  # non-transient: not retried


def test_other_transport_error_is_structured_not_internal(creds, monkeypatch):
    # ChunkedEncodingError is neither Timeout nor ConnectionError; it must still map
    # to a structured error (never escape to the handler's internal_error fallback).
    calls = patch_request(
        monkeypatch,
        lambda m, u, c, **k: (_ for _ in ()).throw(requests.exceptions.ChunkedEncodingError("truncated")),
    )
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("acme/web", {"title": "t"})
    assert ei.value.error == "tracker_unreachable"
    assert len(calls) == clients._MAX_RETRIES + 1


def test_github_429_is_rate_limited(creds, monkeypatch):
    calls = patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(429, {"message": "slow down"}))
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("acme/web", {"title": "t"})
    assert ei.value.error == "rate_limited"
    assert len(calls) == clients._MAX_RETRIES + 1  # rate limits are retried, bounded


def test_github_403_rate_limit_not_invalid_credentials(creds, monkeypatch):
    # GitHub uses 403 for secondary rate limits (with Retry-After / a rate-limit
    # body). It must NOT be reported as 'check your token'.
    patch_request(
        monkeypatch,
        lambda m, u, c, **k: FakeResp(403, {"message": "You have exceeded a secondary rate limit"},
                                      headers={"Retry-After": "1"}),
    )
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("acme/web", {"title": "t"})
    assert ei.value.error == "rate_limited"


def test_plain_403_is_still_invalid_credentials(creds, monkeypatch):
    # A 403 with no rate-limit signal stays an auth error (regression guard).
    patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(403, {"message": "Bad credentials"}))
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("acme/web", {"title": "t"})
    assert ei.value.error == "invalid_credentials"


def test_github_dedup_verifies_title(creds, monkeypatch):
    # Search may fuzzily match; only an exact-title hit is a real duplicate.
    items = {"items": [
        {"html_url": "https://github.com/acme/web/issues/1", "title": "Unrelated thing"},
        {"html_url": "https://github.com/acme/web/issues/2", "title": "Save button overlaps footer"},
    ]}
    patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(200, items))
    client = clients.make_client("github_issues", {"repo": "acme/web"})
    dedup = {"kind": "github_search", "q": "x", "title": "Save button overlaps footer"}
    assert client.find_duplicate(dedup) == "https://github.com/acme/web/issues/2"
    # No title matches -> not a duplicate.
    dedup2 = {"kind": "github_search", "q": "x", "title": "Totally different"}
    assert client.find_duplicate(dedup2) is None


# --- Jira -------------------------------------------------------------------
def test_jira_create_success(creds, monkeypatch):
    def handler(method, url, calls, **kw):
        assert kw["auth"] == ("bot@acme.com", "jira-token")
        return FakeResp(201, {"key": "ENG-1", "id": "1001"})

    patch_request(monkeypatch, handler)
    client = clients.make_client("jira", {"base_url": "https://acme.atlassian.net"})
    out = client.create_issue("ENG", {"fields": {}})
    assert out["url"] == "https://acme.atlassian.net/browse/ENG-1"
    assert out["id"] == "1001"


def test_jira_dedup(creds, monkeypatch):
    def hit(method, url, calls, **kw):
        assert method == "POST"
        assert url.endswith("/rest/api/3/search/jql")
        assert kw["json"]["jql"] == "project = ENG"
        return FakeResp(200, {"issues": [{"key": "ENG-9"}]})

    patch_request(monkeypatch, hit)
    client = clients.make_client("jira", {"base_url": "https://acme.atlassian.net"})
    assert client.find_duplicate({"kind": "jql", "jql": "project = ENG"}) == "https://acme.atlassian.net/browse/ENG-9"

    patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(200, {"issues": []}))
    assert client.find_duplicate({"kind": "jql", "jql": "x"}) is None


def test_jira_401(creds, monkeypatch):
    patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(403, {}))
    client = clients.make_client("jira", {"base_url": "https://acme.atlassian.net"})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("ENG", {"fields": {}})
    assert ei.value.error == "invalid_credentials"
    assert "JIRA_API_TOKEN" in ei.value.remediation


def test_jira_insecure_base_url_rejected(creds):
    with pytest.raises(BugTicketError) as ei:
        clients.make_client("jira", {"base_url": "http://insecure.example.com"})
    assert ei.value.error == "insecure_url"


# --- Linear -----------------------------------------------------------------
def test_linear_create_success(creds, monkeypatch):
    def handler(method, url, calls, **kw):
        assert url == clients._LINEAR_URL
        assert kw["headers"]["Authorization"] == "lin_key"
        return FakeResp(200, {"data": {"issueCreate": {"success": True, "issue": {"id": "iss-1", "url": "https://linear.app/acme/issue/ENG-1"}}}})

    patch_request(monkeypatch, handler)
    client = clients.make_client("linear", {})
    out = client.create_issue("team-1", {"teamId": "team-1", "title": "t"})
    assert out["url"] == "https://linear.app/acme/issue/ENG-1"
    assert out["id"] == "iss-1"


def test_linear_dedup_title_match(creds, monkeypatch):
    nodes = {"data": {"issueSearch": {"nodes": [
        {"id": "1", "url": "https://linear.app/x/issue/A", "title": "Other bug"},
        {"id": "2", "url": "https://linear.app/x/issue/B", "title": "Save button overlaps footer"},
    ]}}}
    patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(200, nodes))
    client = clients.make_client("linear", {})
    assert client.find_duplicate({"kind": "linear", "title": "Save button overlaps footer"}) == "https://linear.app/x/issue/B"
    # No title match -> None
    assert client.find_duplicate({"kind": "linear", "title": "Totally different"}) is None


def test_linear_graphql_auth_error(creds, monkeypatch):
    patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(200, {"errors": [{"message": "Authentication required"}]}))
    client = clients.make_client("linear", {})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("team-1", {"teamId": "team-1", "title": "t"})
    assert ei.value.error == "invalid_credentials"


def test_linear_auth_error_via_extensions(creds, monkeypatch):
    # Message text alone wouldn't match, but the structured extensions.type does.
    body = {"errors": [{"message": "Forbidden resource", "extensions": {"type": "authentication_error"}}]}
    patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(200, body))
    client = clients.make_client("linear", {})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("team-1", {"teamId": "team-1", "title": "t"})
    assert ei.value.error == "invalid_credentials"


def test_linear_non_auth_graphql_error_is_tracker_error(creds, monkeypatch):
    body = {"errors": [{"message": "Field 'foo' is not defined"}]}
    patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(200, body))
    client = clients.make_client("linear", {})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("team-1", {"teamId": "team-1", "title": "t"})
    assert ei.value.error == "tracker_error"


def test_linear_create_success_false_is_tracker_error(creds, monkeypatch):
    body = {"data": {"issueCreate": {"success": False}}}
    patch_request(monkeypatch, lambda m, u, c, **k: FakeResp(200, body))
    client = clients.make_client("linear", {})
    with pytest.raises(BugTicketError) as ei:
        client.create_issue("team-1", {"teamId": "team-1", "title": "t"})
    assert ei.value.error == "tracker_error"


# --- factory / creds --------------------------------------------------------
def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(BugTicketError) as ei:
        clients.make_client("github_issues", {"repo": "acme/web"})
    assert ei.value.error == "missing_credentials"
    assert "GITHUB_TOKEN" in ei.value.remediation


def test_unknown_target():
    with pytest.raises(BugTicketError) as ei:
        clients.make_client("trello", {})
    assert ei.value.error == "unknown_target"
