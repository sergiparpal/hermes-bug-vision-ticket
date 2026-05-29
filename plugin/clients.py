"""REST clients — one create-issue path per tracker, with idempotent dedup.

Each client exposes:
  * ``find_duplicate(dedup) -> existing_url | None`` — read-only search using the
    descriptor from ``mapping.build_dedup`` (skipped when dedup is disabled).
  * ``create_issue(project, payload) -> {"url": ..., "id": ...}``.

Hard rules enforced here:
  * Every request is HTTPS with an explicit timeout. No exceptions.
  * Bounded retries (2) on connection errors / timeouts / 5xx only — never on 4xx.
  * Credentials are read from env ONLY when a client is constructed/used, never
    logged, and never echoed in errors.
  * Status codes map to structured errors: 401/403 -> invalid credentials (the
    remediation names the env var), 404 -> project/repo not found, timeout ->
    tracker unreachable.

``requests`` is a core hermes-agent dependency, so no new dependency is added.
Tests monkeypatch ``clients.requests.request`` — no real network.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import requests

from .errors import BugTicketError

DEFAULT_TIMEOUT = 15.0
_MAX_RETRIES = 2          # total attempts = _MAX_RETRIES + 1
_BACKOFF_SECONDS = 0.3
_RETRYABLE_STATUS = {500, 502, 503, 504}


def _require_env(name: str) -> str:
    import os

    val = os.environ.get(name)
    if not val:
        raise BugTicketError(
            "missing_credentials",
            f"Environment variable {name} is not set. Configure it to use this tracker.",
        )
    return val


def _http(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Any] = None,
    auth: Optional[Tuple[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    creds_hint: str = "",
    notfound_hint: str = "",
) -> requests.Response:
    """Perform one HTTPS request with timeout, bounded retries, and error mapping."""
    if not url.lower().startswith("https://"):
        raise BugTicketError(
            "insecure_url",
            f"Refusing a non-HTTPS request to {url!r}; tracker URLs must use https://.",
        )

    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                auth=auth,
                timeout=timeout,
            )
        except requests.exceptions.Timeout as exc:
            last_exc = exc
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
        else:
            status = resp.status_code
            if status in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_SECONDS * (attempt + 1))
                continue
            if status in (401, 403):
                raise BugTicketError(
                    "invalid_credentials",
                    creds_hint or "Authentication failed; check your tracker credentials.",
                )
            if status == 404:
                raise BugTicketError(
                    "not_found",
                    notfound_hint or "The requested project/repository was not found.",
                )
            if status >= 400:
                raise BugTicketError(
                    "tracker_error",
                    f"Tracker returned HTTP {status}. {_short_body(resp)}",
                )
            return resp

        if attempt < _MAX_RETRIES:
            time.sleep(_BACKOFF_SECONDS * (attempt + 1))

    # Exhausted retries on a transport error.
    if isinstance(last_exc, requests.exceptions.Timeout):
        raise BugTicketError("tracker_timeout", "The tracker did not respond in time; try again later.")
    raise BugTicketError("tracker_unreachable", "Could not reach the tracker; check connectivity.")


def _short_body(resp: requests.Response, limit: int = 200) -> str:
    try:
        text = resp.text or ""
    except Exception:
        return ""
    text = text.strip().replace("\n", " ")
    return text[:limit]


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------
class JiraClient:
    """Jira Cloud REST v3 (basic auth: email + API token)."""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        base = (cfg.get("base_url") or "").strip().rstrip("/")
        if not base:
            base = _require_env("JIRA_BASE_URL").rstrip("/")
        self.base_url = base
        self.email = _require_env("JIRA_EMAIL")
        self.token = _require_env("JIRA_API_TOKEN")
        if not self.base_url.lower().startswith("https://"):
            raise BugTicketError(
                "insecure_url",
                "JIRA_BASE_URL / base_url must start with https://.",
            )

    @property
    def _auth(self) -> Tuple[str, str]:
        return (self.email, self.token)

    _CREDS = "Check JIRA_EMAIL and JIRA_API_TOKEN."

    def find_duplicate(self, dedup: Dict[str, Any]) -> Optional[str]:
        if dedup.get("kind") != "jql":
            return None
        resp = _http(
            "POST",
            f"{self.base_url}/rest/api/3/search/jql",
            headers={"Accept": "application/json"},
            json_body={"jql": dedup["jql"], "maxResults": 1, "fields": ["key"]},
            auth=self._auth,
            creds_hint=self._CREDS,
            notfound_hint="Jira search endpoint not found; check JIRA_BASE_URL.",
        )
        issues = (resp.json() or {}).get("issues") or []
        if not issues:
            return None
        return f"{self.base_url}/browse/{issues[0]['key']}"

    def create_issue(self, project: str, payload: Dict[str, Any]) -> Dict[str, str]:
        resp = _http(
            "POST",
            f"{self.base_url}/rest/api/3/issue",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json_body=payload,
            auth=self._auth,
            creds_hint=self._CREDS,
            notfound_hint=f"Jira project '{project}' or issue type not found.",
        )
        data = resp.json() or {}
        key = data.get("key", "")
        return {"url": f"{self.base_url}/browse/{key}", "id": str(data.get("id", key))}


# ---------------------------------------------------------------------------
# Linear (GraphQL)
# ---------------------------------------------------------------------------
_LINEAR_URL = "https://api.linear.app/graphql"
_LINEAR_CREATE = (
    "mutation($input: IssueCreateInput!) {"
    " issueCreate(input: $input) { success issue { id identifier url } } }"
)
_LINEAR_SEARCH = (
    "query($term: String!) {"
    " issueSearch(query: $term, first: 5) { nodes { id url title } } }"
)


class LinearClient:
    """Linear GraphQL API (personal API key in the Authorization header)."""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.api_key = _require_env("LINEAR_API_KEY")

    _CREDS = "Check LINEAR_API_KEY."

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": self.api_key, "Content-Type": "application/json"}

    def _graphql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        resp = _http(
            "POST",
            _LINEAR_URL,
            headers=self._headers(),
            json_body={"query": query, "variables": variables},
            creds_hint=self._CREDS,
        )
        body = resp.json() or {}
        if body.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in body["errors"])[:200]
            # Linear reports auth failures as GraphQL errors with HTTP 200.
            if "authentication" in msg.lower() or "unauthorized" in msg.lower():
                raise BugTicketError("invalid_credentials", self._CREDS)
            raise BugTicketError("tracker_error", f"Linear API error: {msg}")
        return body.get("data") or {}

    def find_duplicate(self, dedup: Dict[str, Any]) -> Optional[str]:
        if dedup.get("kind") != "linear":
            return None
        title = (dedup.get("title") or "").strip()
        if not title:
            return None
        data = self._graphql(_LINEAR_SEARCH, {"term": title})
        nodes = (data.get("issueSearch") or {}).get("nodes") or []
        for node in nodes:
            if (node.get("title") or "").strip().lower() == title.lower():
                return node.get("url")
        return None

    def create_issue(self, project: str, payload: Dict[str, Any]) -> Dict[str, str]:
        data = self._graphql(_LINEAR_CREATE, {"input": payload})
        result = data.get("issueCreate") or {}
        if not result.get("success"):
            raise BugTicketError("tracker_error", "Linear issueCreate did not succeed.")
        issue = result.get("issue") or {}
        return {"url": issue.get("url", ""), "id": issue.get("id", issue.get("identifier", ""))}


# ---------------------------------------------------------------------------
# GitHub Issues (REST)
# ---------------------------------------------------------------------------
_GITHUB_API = "https://api.github.com"


class GitHubClient:
    """GitHub Issues REST API (token bearer auth)."""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.token = _require_env("GITHUB_TOKEN")

    _CREDS = "Check GITHUB_TOKEN (needs 'repo'/'issues' scope)."

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def find_duplicate(self, dedup: Dict[str, Any]) -> Optional[str]:
        if dedup.get("kind") != "github_search":
            return None
        resp = _http(
            "GET",
            f"{_GITHUB_API}/search/issues",
            headers=self._headers(),
            params={"q": dedup["q"], "per_page": 1},
            creds_hint=self._CREDS,
        )
        items = (resp.json() or {}).get("items") or []
        if not items:
            return None
        return items[0].get("html_url")

    def create_issue(self, project: str, payload: Dict[str, Any]) -> Dict[str, str]:
        resp = _http(
            "POST",
            f"{_GITHUB_API}/repos/{project}/issues",
            headers=self._headers(),
            json_body=payload,
            creds_hint=self._CREDS,
            notfound_hint=f"GitHub repo '{project}' not found, or token lacks access.",
        )
        data = resp.json() or {}
        return {"url": data.get("html_url", ""), "id": str(data.get("number", data.get("id", "")))}


_CLIENTS = {
    "jira": JiraClient,
    "linear": LinearClient,
    "github_issues": GitHubClient,
}


def make_client(target: str, target_cfg: Dict[str, Any]):
    """Construct the client for ``target`` (reads + validates its env credentials)."""
    cls = _CLIENTS.get(target)
    if cls is None:
        raise BugTicketError("unknown_target", f"No client for target '{target}'.")
    return cls(target_cfg)
