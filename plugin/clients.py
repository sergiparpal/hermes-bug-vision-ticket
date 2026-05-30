"""REST clients — one create-issue path per tracker, with idempotent dedup.

Each client exposes:
  * ``find_duplicate(dedup) -> existing_url | None`` — read-only search using the
    descriptor from ``mapping.build_dedup`` (skipped when dedup is disabled).
  * ``create_issue(project, payload) -> {"url": ..., "id": ...}``.

Hard rules enforced here:
  * Every request is HTTPS with an explicit timeout. No exceptions.
  * Bounded retries (2) on connection errors / timeouts / 5xx / rate limits only —
    never on a 4xx that is not a rate limit. Any other transport error is mapped to
    a structured error too (never escapes as a generic internal_error).
  * Credentials are read from env ONLY when a client is constructed/used, never
    logged, and never echoed in errors.
  * Status codes map to structured errors: 401/403 -> invalid credentials (the
    remediation names the env var), 404 -> project/repo not found, 403/429 with a
    rate-limit signal -> rate_limited (Retry-After honored), TLS failure ->
    tls_error, timeout -> tracker_timeout, other transport failure -> unreachable.

``requests`` is a core hermes-agent dependency, so no new dependency is added.
Tests monkeypatch ``clients.requests.request`` — no real network.
"""

from __future__ import annotations

import ipaddress
import os
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from .errors import BugTicketError

DEFAULT_TIMEOUT = 15.0
_MAX_RETRIES = 2          # total attempts = _MAX_RETRIES + 1
_BACKOFF_SECONDS = 0.3
_MAX_RETRY_AFTER = 20.0   # cap an honored Retry-After so a tool call can't block long
_RETRYABLE_STATUS = {500, 502, 503, 504}
# Control chars (incl. C1) are scrubbed from any tracker body we echo back: the body
# is untrusted/attacker-influenceable and ends up in a remediation string.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _check_request_host(url: str) -> None:
    """Refuse a request whose host is a loopback/link-local IP literal (SSRF).

    The Linear/GitHub hosts are fixed public endpoints; the only operator-tunable
    host is the Jira ``base_url``. We can't fully prevent a malicious config from
    pointing at an internal host (and self-hosted Jira legitimately lives on
    private/RFC1918 addresses, so blocking those would break real deployments),
    but loopback (127.0.0.0/8, ::1) and link-local (169.254.0.0/16 — incl. the
    cloud metadata endpoint 169.254.169.254, fe80::/10) are NEVER a legitimate
    tracker and are the classic SSRF targets, so we hard-block those literals.

    Only IP *literals* are checked — a hostname that resolves to a blocked range
    (DNS rebinding) is out of scope here; the operator-trusted config threat model
    plus allow_redirects=False (below) make literal-blocking the proportionate guard.
    """
    host = (urlsplit(url).hostname or "").strip()
    if not host:
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # a hostname, not an IP literal -> allowed (not resolved here)
    if ip.is_loopback or ip.is_link_local:
        raise BugTicketError(
            "blocked_host",
            f"Refusing a request to {host!r}: loopback/link-local addresses are not "
            "permitted tracker hosts. Point base_url at the tracker's real host.",
        )


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise BugTicketError(
            "missing_credentials",
            f"Environment variable {name} is not set. Configure it to use this tracker.",
        )
    return val


def _resp_headers(resp: requests.Response) -> dict[str, Any]:
    """Headers as a mapping, tolerant of mocks that omit the attribute."""
    return getattr(resp, "headers", None) or {}


def _backoff(attempt: int) -> float:
    return _BACKOFF_SECONDS * (attempt + 1)


def _retry_after(resp: requests.Response, attempt: int) -> float:
    """Honor a numeric Retry-After (capped); fall back to linear backoff."""
    raw = _resp_headers(resp).get("Retry-After")
    if raw:
        try:
            secs = float(str(raw).strip())
        except ValueError:
            secs = -1.0  # HTTP-date form — not parsed; use backoff
        if secs >= 0:
            return min(secs, _MAX_RETRY_AFTER)
    return _backoff(attempt)


def _is_rate_limited(resp: requests.Response) -> bool:
    """True for a rate-limit response. 429 always; 403 only with a rate-limit signal.

    GitHub signals primary/secondary rate limits with 403 OR 429 (+ Retry-After /
    X-RateLimit-Remaining: 0 / a rate-limit body); a plain 403 stays an auth error.
    """
    status = resp.status_code
    if status == 429:
        return True
    if status == 403:
        headers = _resp_headers(resp)
        if headers.get("Retry-After"):
            return True
        if str(headers.get("X-RateLimit-Remaining", "")).strip() == "0":
            return True
        body = _short_body(resp).lower()
        if "rate limit" in body or "secondary rate" in body or "abuse" in body:
            return True
    return False


def _raise_for_status(
    resp: requests.Response, *, creds_hint: str, notfound_hint: str
) -> requests.Response:
    """Interpret a final (non-retryable, non-rate-limited) HTTP response.

    Returns the response on success, else raises the matching structured error.
    Rate-limit handling stays in ``_http`` because it depends on retry state; this
    covers the terminal status ladder so ``_http`` is about transport + retries and
    this is about what a status code *means*.
    """
    status = resp.status_code
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
    if 300 <= status < 400:
        # We deliberately do not follow redirects (see allow_redirects in _http). A
        # redirect almost always means base_url is wrong (e.g. http->https upgrade,
        # trailing-host change), so fail with a clear remediation.
        raise BugTicketError(
            "tracker_redirect",
            f"Tracker responded with a redirect (HTTP {status}); redirects are "
            "not followed. Check base_url points directly at the tracker API.",
        )
    if status >= 400:
        raise BugTicketError(
            "tracker_error",
            f"Tracker returned HTTP {status}. {_echo(_short_body(resp))}",
        )
    return resp


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
    auth: tuple[str, str] | None = None,
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
    _check_request_host(url)

    last_exc: Exception | None = None
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
                # Do not follow redirects: these REST/GraphQL APIs never need them,
                # and a redirect could bounce an authenticated request to an
                # unexpected (e.g. internal) host. Keeps the request on the
                # https-validated, host-checked URL we constructed.
                allow_redirects=False,
            )
        except requests.exceptions.Timeout as exc:
            last_exc = exc
        except requests.exceptions.SSLError as exc:
            # TLS/certificate failure: non-transient and retrying cannot fix it.
            # Map distinctly so the remediation points at the cert/base_url, not
            # connectivity (and so it is not retried).
            raise BugTicketError(
                "tls_error",
                "TLS/certificate verification failed for the tracker host; check its "
                "certificate or base_url.",
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
        except requests.exceptions.RequestException as exc:
            # Any other transport-layer failure (chunked-encoding, content-decoding,
            # too-many-redirects, …). Catch the base class so no network error escapes
            # to the handler's generic internal_error fallback.
            last_exc = exc
        else:
            status = resp.status_code
            rate_limited = _is_rate_limited(resp)
            if (rate_limited or status in _RETRYABLE_STATUS) and attempt < _MAX_RETRIES:
                time.sleep(_retry_after(resp, attempt) if rate_limited else _backoff(attempt))
                continue
            if rate_limited:
                raise BugTicketError(
                    "rate_limited",
                    "The tracker is rate-limiting requests; wait and retry, and reduce "
                    "the request rate.",
                )
            return _raise_for_status(resp, creds_hint=creds_hint, notfound_hint=notfound_hint)

        if attempt < _MAX_RETRIES:
            time.sleep(_backoff(attempt))

    # Exhausted retries on a transport error.
    if isinstance(last_exc, requests.exceptions.Timeout):
        raise BugTicketError("tracker_timeout", "The tracker did not respond in time; try again later.")
    if isinstance(last_exc, requests.exceptions.ConnectionError):
        raise BugTicketError("tracker_unreachable", "Could not reach the tracker; check connectivity.")
    kind = type(last_exc).__name__ if last_exc else "unknown error"
    raise BugTicketError("tracker_unreachable", f"The tracker request failed ({kind}); check connectivity.")


def _sanitize_echo(text: str, limit: int = 200) -> str:
    """Make untrusted text safe to echo into a remediation: scrub control chars,
    collapse whitespace, and clip. Shared by every place that surfaces tracker text."""
    return " ".join(_CONTROL_CHARS.sub(" ", text).split())[:limit]


def _short_body(resp: requests.Response, limit: int = 200) -> str:
    try:
        text = resp.text or ""
    except Exception:
        return ""
    # The body is untrusted and is echoed into a remediation, so sanitize it.
    return _sanitize_echo(text, limit)


def _echo(body: str) -> str:
    """Wrap an echoed tracker message with an explicit untrusted marker.

    The body/message is attacker-influenceable (a malicious or compromised
    tracker can put arbitrary natural-language text in it) and ends up in a
    ``remediation`` string handed back to the agent — a second-order prompt-
    injection channel. Control chars are already scrubbed in ``_short_body``;
    this prefix tells the reader (and the agent) the content is data, not
    instructions to follow.
    """
    return f"[untrusted tracker output] {body}" if body else ""


def _json_body(resp: requests.Response) -> dict[str, Any]:
    """Parse a JSON object body, mapping a non-JSON 2xx response to a clean error.

    ``resp.json()`` raises ``requests...JSONDecodeError`` (a ``ValueError``) on a
    non-JSON/empty body — without this it would escape the structured-error
    mapping and surface as a generic internal_error. Non-dict JSON is treated as
    an empty mapping (callers only read keys).
    """
    try:
        data = resp.json()
    except ValueError as exc:
        raise BugTicketError(
            "tracker_error",
            f"Tracker returned a non-JSON {resp.status_code} response. {_echo(_short_body(resp))}",
        ) from exc
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------
class JiraClient:
    """Jira Cloud REST v3 (basic auth: email + API token)."""

    def __init__(self, cfg: dict[str, Any]) -> None:
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
    def _auth(self) -> tuple[str, str]:
        return (self.email, self.token)

    _CREDS = "Check JIRA_EMAIL and JIRA_API_TOKEN."

    def find_duplicate(self, dedup: dict[str, Any]) -> str | None:
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
        issues = _json_body(resp).get("issues") or []
        if not issues:
            return None
        return f"{self.base_url}/browse/{issues[0]['key']}"

    def create_issue(self, project: str, payload: dict[str, Any]) -> dict[str, str]:
        resp = _http(
            "POST",
            f"{self.base_url}/rest/api/3/issue",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json_body=payload,
            auth=self._auth,
            creds_hint=self._CREDS,
            notfound_hint=f"Jira project '{project}' or issue type not found.",
        )
        data = _json_body(resp)
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

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.api_key = _require_env("LINEAR_API_KEY")

    _CREDS = "Check LINEAR_API_KEY."

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.api_key, "Content-Type": "application/json"}

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        resp = _http(
            "POST",
            _LINEAR_URL,
            headers=self._headers(),
            json_body={"query": query, "variables": variables},
            creds_hint=self._CREDS,
        )
        body = _json_body(resp)
        errors = body.get("errors")
        if errors:
            # GraphQL error text is untrusted and echoed into a remediation -> sanitize.
            msg = _sanitize_echo("; ".join(str(e.get("message", e)) for e in errors))
            # Linear reports auth failures as GraphQL errors with HTTP 200. Detect
            # via the structured error extensions (type/code) when present — the
            # message text is not contractual — falling back to message keywords.
            blob = msg.lower()
            for e in errors:
                ext = e.get("extensions") if isinstance(e, dict) else None
                if isinstance(ext, dict):
                    blob += " " + str(ext.get("type", "")).lower() + " " + str(ext.get("code", "")).lower()
            if any(
                k in blob
                for k in ("authentication", "unauthorized", "not authorized",
                          "forbidden", "invalid api key", "api key")
            ):
                raise BugTicketError("invalid_credentials", self._CREDS)
            raise BugTicketError("tracker_error", f"Linear API error: {_echo(msg)}")
        return body.get("data") or {}

    def find_duplicate(self, dedup: dict[str, Any]) -> str | None:
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

    def create_issue(self, project: str, payload: dict[str, Any]) -> dict[str, str]:
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

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.token = _require_env("GITHUB_TOKEN")

    _CREDS = "Check GITHUB_TOKEN (needs 'repo'/'issues' scope)."

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def find_duplicate(self, dedup: dict[str, Any]) -> str | None:
        if dedup.get("kind") != "github_search":
            return None
        resp = _http(
            "GET",
            f"{_GITHUB_API}/search/issues",
            headers=self._headers(),
            params={"q": dedup["q"], "per_page": 10},
            creds_hint=self._CREDS,
        )
        items = _json_body(resp).get("items") or []
        # Verify the matched issue's title actually equals the expected title before
        # treating it as a duplicate (mirrors Linear). GitHub search is fuzzy and the
        # query is built from untrusted model text, so a bare top-hit could be an
        # unrelated issue; an exact-title check prevents a false dedup / wrong URL.
        expected = (dedup.get("title") or "").strip().lower()
        for item in items:
            if not expected or (item.get("title") or "").strip().lower() == expected:
                return item.get("html_url")
        return None

    def create_issue(self, project: str, payload: dict[str, Any]) -> dict[str, str]:
        resp = _http(
            "POST",
            # project is validated to owner/name in mapping._resolve_project; quote
            # each path segment as defense-in-depth so it can never alter the path.
            f"{_GITHUB_API}/repos/{quote(project, safe='/')}/issues",
            headers=self._headers(),
            json_body=payload,
            creds_hint=self._CREDS,
            notfound_hint=f"GitHub repo '{project}' not found, or token lacks access.",
        )
        data = _json_body(resp)
        return {"url": data.get("html_url", ""), "id": str(data.get("number", data.get("id", "")))}


_CLIENTS = {
    "jira": JiraClient,
    "linear": LinearClient,
    "github_issues": GitHubClient,
}


def make_client(target: str, target_cfg: dict[str, Any]):
    """Construct the client for ``target`` (reads + validates its env credentials)."""
    cls = _CLIENTS.get(target)
    if cls is None:
        raise BugTicketError("unknown_target", f"No client for target '{target}'.")
    return cls(target_cfg)
