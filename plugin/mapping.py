"""Map a normalized BugReport + target config -> a tracker-specific payload.

This is the load-bearing part of the plugin: per-tracker field mapping is the
hard 90%, "POST to an API" is the easy 10%. Everything here is a PURE function
(no network, no env, no I/O) so it is fully unit-testable.

Per tracker we produce the create-issue request body:
  * Jira   -> {"fields": {...}}   with an ADF description (Jira Cloud /api/3).
  * Linear -> issueCreate `input` {...}  with a markdown description.
  * GitHub -> {"title","body","labels"}  with a markdown body.

Severity is resolved through the config's ``severity_map`` (no guessing — an
unmapped severity is an error). ``{placeholder}`` templates in custom fields and
dedup queries are expanded from the BugReport; an unknown placeholder is an
error.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import BugTicketError
from .schemas import SUPPORTED_TARGETS

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_JIRA_SUMMARY_MAX = 255

# Control chars / newlines have no place in a one-line JQL string literal and are a
# multiline-breakout aid, so they are stripped from interpolated (untrusted) values
# before the backslash/quote escaping in build_dedup. NOTE: placeholders in a
# jql_template MUST sit inside a double-quoted literal (e.g. summary ~ "{title}") —
# the quote/backslash escaping only protects a quoted position; an unquoted
# placeholder is unsafe regardless of escaping. See build_dedup + README.
_JQL_STRIP = re.compile(r"[\x00-\x1f\x7f]")

# Markdown metacharacters escaped in UNTRUSTED, OCR-derived body text so a crafted
# screenshot cannot inject the high-impact constructs into a GitHub/Linear issue
# body: links / image beacons (need '[' ']' '!'), inline HTML ('<' '>'), and code
# spans ('`'); '\' is escaped first so our own escaping can't be subverted. These
# are GFM/CommonMark escapable punctuation, so they render literally with no visible
# backslash. We deliberately do NOT escape '.', '-', '*', '_', '#', etc.: escaping
# them would add backslash noise to ordinary prose, and the residue they'd address
# (bare-URL autolinks, cosmetic emphasis/headings) is visible + click-gated, not a
# beacon. (The Jira ADF path renders text nodes verbatim and is inert -> not escaped.)
_MD_ESCAPE = re.compile(r"([\\`\[\]<>!])")

# A Jira project key must be safe to drop into an UNQUOTED JQL position (the
# standard dedup template uses `project = {project_key}`), where the quoted-literal
# escaper does nothing — so restrict it to letters/digits/underscore.
_JIRA_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# A GitHub repo segment (owner or name): alphanumeric start, then word/./-; this
# rejects '', '..', spaces, and URL-control chars so it is safe in the API path.
_GH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# GitHub search context keys that hold structural (validated) values; everything
# else in the template context is untrusted model text and is sanitized for search.
_GH_STRUCTURAL_KEYS = frozenset({"project", "project_key", "team_id", "repo"})
# GitHub search qualifier delimiters / quoting that untrusted text must not inject.
_GH_QUALIFIER_CHARS = re.compile(r'[":]')

# Core fields a per-target builder owns; a severity_map / custom_fields entry must
# not silently overwrite them (a colliding key is a config error, not a clobber).
_JIRA_RESERVED = frozenset({"project", "issuetype", "summary", "description"})
_LINEAR_RESERVED = frozenset({"teamId", "title", "description"})


# ---------------------------------------------------------------------------
# Template expansion
# ---------------------------------------------------------------------------
def _template_context(bug_report: dict[str, Any], project: str) -> dict[str, str]:
    """Flat string context for {placeholder} expansion. None -> ''."""
    def s(key: str) -> str:
        v = bug_report.get(key)
        return "" if v is None else str(v)

    return {
        "title": s("title"),
        "summary": s("summary"),
        "severity": s("severity"),
        "confidence": s("confidence"),
        "component_hint": s("component_hint"),
        "expected_behavior": s("expected_behavior"),
        "actual_behavior": s("actual_behavior"),
        "project": project,
        "project_key": project,
        "team_id": project,
        "repo": project,
    }


def _expand_str(template: str, tmpl_ctx: dict[str, str]) -> str:
    def repl(m: "re.Match[str]") -> str:
        key = m.group(1)
        if key not in tmpl_ctx:
            raise BugTicketError(
                "missing_placeholder",
                f"Template references unknown placeholder '{{{key}}}'. Known: "
                f"{', '.join(sorted(tmpl_ctx))}.",
            )
        return tmpl_ctx[key]

    return _PLACEHOLDER.sub(repl, template)


def _expand_value(value: Any, tmpl_ctx: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _expand_str(value, tmpl_ctx)
    if isinstance(value, list):
        return [_expand_value(v, tmpl_ctx) for v in value]
    if isinstance(value, dict):
        return {k: _expand_value(v, tmpl_ctx) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Body composition (shared sections -> ADF or markdown)
# ---------------------------------------------------------------------------
def _sections(bug_report: dict[str, Any]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []

    summary = (bug_report.get("summary") or "").strip()
    if summary:
        sections.append({"heading": None, "kind": "para", "content": summary})

    steps = [str(x).strip() for x in (bug_report.get("steps_to_reproduce") or []) if str(x).strip()]
    if steps:
        sections.append({"heading": "Steps to reproduce", "kind": "list", "content": steps})

    expected = (bug_report.get("expected_behavior") or "").strip()
    if expected:
        sections.append({"heading": "Expected behavior", "kind": "para", "content": expected})

    actual = (bug_report.get("actual_behavior") or "").strip()
    if actual:
        sections.append({"heading": "Actual behavior", "kind": "para", "content": actual})

    component = (bug_report.get("component_hint") or "").strip()
    if component:
        sections.append({"heading": None, "kind": "para", "content": f"Affected component: {component}"})

    ui = [str(x).strip() for x in (bug_report.get("ui_elements_observed") or []) if str(x).strip()]
    if ui:
        sections.append({"heading": "Observed UI elements", "kind": "list", "content": ui})

    # visible_text is OCR'd from the screenshot and is UNTRUSTED; render it as a
    # clearly-labelled data block (never as instructions) so the model's extracted
    # evidence reaches the ticket without being dropped.
    seen = [str(x).strip() for x in (bug_report.get("visible_text") or []) if str(x).strip()]
    if seen:
        sections.append({"heading": "Text observed in screenshot (as-read)", "kind": "list", "content": seen})

    confidence = bug_report.get("confidence", "medium")
    sections.append({
        "heading": None,
        "kind": "para",
        # Plugin-authored, not model text -> trusted -> rendered without Markdown escaping.
        "trusted": True,
        "content": f"Filed from a screenshot via hermes-bug-vision-ticket (model confidence: {confidence}).",
    })
    return sections


def _md_escape(text: str) -> str:
    """Backslash-escape Markdown metacharacters in untrusted free text (see _MD_ESCAPE)."""
    return _MD_ESCAPE.sub(r"\\\1", text)


def _render_markdown(bug_report: dict[str, Any]) -> str:
    out: list[str] = []
    for sec in _sections(bug_report):
        if sec["heading"]:
            out.append(f"## {sec['heading']}")
        # Section content is untrusted (model-extracted from the screenshot) unless
        # explicitly marked trusted (the plugin-authored footer); escape the former so
        # injected Markdown renders as inert text in the GitHub/Linear issue body.
        raw = bool(sec.get("trusted"))
        if sec["kind"] == "para":
            out.append(sec["content"] if raw else _md_escape(sec["content"]))
        else:
            out.extend(
                f"- {item if raw else _md_escape(item)}" for item in sec["content"]
            )
        out.append("")
    return "\n".join(out).strip()


def _render_adf(bug_report: dict[str, Any]) -> dict[str, Any]:
    """Minimal Atlassian Document Format doc (Jira Cloud /rest/api/3 description)."""
    content: list[dict[str, Any]] = []
    for sec in _sections(bug_report):
        if sec["heading"]:
            content.append({
                "type": "heading",
                "attrs": {"level": 3},
                "content": [{"type": "text", "text": sec["heading"]}],
            })
        if sec["kind"] == "para":
            content.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": sec["content"]}],
            })
        else:
            items = [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": item}]}
                    ],
                }
                for item in sec["content"]
            ]
            content.append({"type": "bulletList", "content": items})
    return {"type": "doc", "version": 1, "content": content}


# ---------------------------------------------------------------------------
# Helpers shared by per-target builders
# ---------------------------------------------------------------------------
def _resolve_project(target: str, cfg: dict[str, Any], override: str | None) -> str:
    field = {"jira": "project_key", "linear": "team_id", "github_issues": "repo"}[target]
    value = (override or cfg.get(field) or "").strip()
    if not value:
        raise BugTicketError(
            "missing_project",
            f"No project for target '{target}'. Set '{field}' in bug-tickets.yaml "
            "or pass project=.",
        )
    if target == "jira" and not _JIRA_KEY_RE.match(value):
        raise BugTicketError(
            "invalid_project_key",
            f"Jira project key '{value}' is invalid; expected letters/digits/underscore "
            "starting with a letter (no spaces or JQL operators).",
        )
    if target == "github_issues":
        owner, sep, name = value.partition("/")
        if not (sep and _GH_SEGMENT_RE.match(owner) and _GH_SEGMENT_RE.match(name)):
            raise BugTicketError(
                "invalid_repo",
                f"GitHub repo must be 'owner/name' (alphanumeric, '.', '_', '-'); got '{value}'.",
            )
    return value


def _severity_entry(cfg: dict[str, Any], severity: str, target: str) -> dict[str, Any]:
    severity_map = cfg.get("severity_map")
    if not isinstance(severity_map, dict) or not severity_map:
        raise BugTicketError(
            "severity_map_missing",
            f"Target '{target}' has no severity_map; add one mapping each of "
            "blocker/critical/major/minor/trivial.",
        )
    entry = severity_map.get(severity)
    if entry is None:
        raise BugTicketError(
            "unmapped_severity",
            f"Severity '{severity}' is not mapped for target '{target}'. Add it to "
            "severity_map.",
        )
    if not isinstance(entry, dict):
        raise BugTicketError(
            "invalid_severity_map",
            f"severity_map['{severity}'] for '{target}' must be a mapping of fields.",
        )
    return entry


def _guard_reserved(extra: dict[str, Any], reserved: frozenset, source: str, target: str) -> None:
    """Refuse a config-supplied field block that would overwrite a core field.

    Without this, a severity_map / custom_fields entry whose key collides with a
    builder-owned field (e.g. summary/description/project) would silently clobber it
    via dict.update — corrupting the payload (bad ADF, wrong team) and surfacing only
    as an opaque tracker 400. Fail loudly with a structured error instead.
    """
    bad = sorted(k for k in extra if k in reserved)
    if bad:
        raise BugTicketError(
            "reserved_field_override",
            f"{source} for target '{target}' may not set reserved field(s): "
            f"{', '.join(bad)}. Rename or remove them.",
        )


# ---------------------------------------------------------------------------
# Per-target payload builders
# ---------------------------------------------------------------------------
def _jira_payload(
    bug_report: dict[str, Any], cfg: dict[str, Any], project: str, tmpl_ctx: dict[str, str]
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "project": {"key": project},
        "issuetype": {"name": cfg.get("issue_type", "Bug")},
        "summary": (bug_report["title"] or "")[:_JIRA_SUMMARY_MAX],
        "description": _render_adf(bug_report),
    }
    labels = cfg.get("labels")
    if labels:
        fields["labels"] = [str(x) for x in labels]

    severity_entry = _severity_entry(cfg, bug_report["severity"], "jira")
    _guard_reserved(severity_entry, _JIRA_RESERVED, "severity_map entry", "jira")
    fields.update(severity_entry)

    # custom_fields is Jira-only: Jira issues accept arbitrary field keys, so we
    # expand {placeholder}s into them. Linear/GitHub have fixed create shapes and
    # deliberately do NOT honor custom_fields (see their builders).
    custom_fields = cfg.get("custom_fields") or {}
    _guard_reserved(custom_fields, _JIRA_RESERVED, "custom_fields", "jira")
    for key, value in custom_fields.items():
        fields[key] = _expand_value(value, tmpl_ctx)

    return {"fields": fields}


def _linear_payload(
    bug_report: dict[str, Any], cfg: dict[str, Any], project: str, tmpl_ctx: dict[str, str]
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "teamId": project,
        "title": bug_report["title"],
        "description": _render_markdown(bug_report),
    }
    severity_entry = _severity_entry(cfg, bug_report["severity"], "linear")
    _guard_reserved(severity_entry, _LINEAR_RESERVED, "severity_map entry", "linear")
    payload.update(severity_entry)

    # No custom_fields support here: Linear's issueCreate takes a fixed input shape
    # (+ labelIds), not arbitrary keys, so only Jira maps custom_fields.
    # Linear's issueCreate takes labelIds (UUIDs), NOT display-name labels, so only
    # honor label_ids — never fall back to the generic `labels` key (which would put
    # names where Linear expects UUIDs and fail the mutation with an opaque error).
    label_ids = cfg.get("label_ids")
    if label_ids:
        payload["labelIds"] = [str(x) for x in label_ids]
    return payload


def _github_payload(
    bug_report: dict[str, Any], cfg: dict[str, Any], project: str, tmpl_ctx: dict[str, str]
) -> dict[str, Any]:
    # No custom_fields support here: a GitHub issue is title/body/labels only, so
    # only Jira maps custom_fields. Severity contributes labels (below).
    labels: list[str] = [str(x) for x in (cfg.get("default_labels") or [])]
    entry = _severity_entry(cfg, bug_report["severity"], "github_issues")
    for label in entry.get("labels", []) or []:
        if label not in labels:
            labels.append(str(label))

    payload: dict[str, Any] = {
        "title": bug_report["title"],
        "body": _render_markdown(bug_report),
    }
    if labels:
        payload["labels"] = labels
    return payload


_BUILDERS = {
    "jira": _jira_payload,
    "linear": _linear_payload,
    "github_issues": _github_payload,
}


def to_payload(
    bug_report: dict[str, Any],
    target: str,
    target_cfg: dict[str, Any],
    project: str | None = None,
) -> dict[str, Any]:
    """Build the create-issue request for ``target``.

    Returns {"target", "project", "title", "create_payload"}. Raises
    BugTicketError on unknown target, missing project, or severity/placeholder
    problems.
    """
    if target not in SUPPORTED_TARGETS:
        raise BugTicketError(
            "unknown_target",
            f"Unknown target '{target}'. Supported: {', '.join(SUPPORTED_TARGETS)}.",
        )
    if not bug_report.get("title"):
        raise BugTicketError("missing_title", "BugReport has no title to file.")

    resolved = _resolve_project(target, target_cfg, project)
    tmpl_ctx = _template_context(bug_report, resolved)
    create_payload = _BUILDERS[target](bug_report, target_cfg, resolved, tmpl_ctx)

    return {
        "target": target,
        "project": resolved,
        "title": bug_report["title"],
        "create_payload": create_payload,
    }


# ---------------------------------------------------------------------------
# Dedup query building (template expansion lives here; the network call is in clients)
# ---------------------------------------------------------------------------
def build_dedup(
    bug_report: dict[str, Any],
    target: str,
    target_cfg: dict[str, Any],
    project: str,
) -> dict[str, Any] | None:
    """Build the dedup search descriptor for a target, or None if disabled.

    Returns one of:
      {"kind": "jql", "jql": "..."}                          (Jira)
      {"kind": "github_search", "q": "...", "title": "..."}   (GitHub; client also verifies title)
      {"kind": "linear", "title": "..."}                      (Linear; client matches by title)
    """
    dedup = target_cfg.get("dedup")
    if not isinstance(dedup, dict) or not dedup.get("enabled"):
        return None

    if target == "jira":
        template = dedup.get("jql_template")
        if not template:
            return None
        # Every interpolated value (all model-extracted, hence untrusted) is escaped
        # for a DOUBLE-QUOTED JQL string literal (see _jql_escape). The jql_template
        # MUST place each {placeholder} inside double quotes (e.g. summary ~
        # "{title}") — the escaping only protects a quoted position.
        tmpl_ctx = _template_context(bug_report, project)
        tmpl_ctx = {k: _jql_escape(v) for k, v in tmpl_ctx.items()}
        return {"kind": "jql", "jql": _expand_str(template, tmpl_ctx).strip()}

    if target == "github_issues":
        template = dedup.get("search_template")
        if not template:
            return None
        # Untrusted (model-extracted) free-text must not inject GitHub search
        # qualifiers (e.g. a second `repo:` or `is:open`); strip the qualifier
        # delimiter ':' and quotes from non-structural fields. The expected title is
        # also returned so the client can verify the matched issue (see find_duplicate).
        tmpl_ctx = _github_search_safe(_template_context(bug_report, project))
        return {
            "kind": "github_search",
            "q": _expand_str(template, tmpl_ctx).strip(),
            "title": (bug_report.get("title") or "").strip(),
        }

    if target == "linear":
        return {"kind": "linear", "title": bug_report.get("title", "")}

    return None


def _jql_escape(value: str) -> str:
    """Escape an untrusted value for a DOUBLE-QUOTED JQL string literal.

    Strips control chars/newlines first (never valid in a one-line literal, and a
    multiline-breakout aid), then escapes backslash FIRST and the double quote
    SECOND so a trailing backslash can't terminate the literal early. Only safe in
    a quoted position — jql_template placeholders must be quoted (see README).
    """
    value = _JQL_STRIP.sub(" ", value)
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _github_search_safe(tmpl_ctx: dict[str, str]) -> dict[str, str]:
    """Neutralize GitHub-search syntax in untrusted free-text context values.

    Structural keys (the validated project, in its various aliases) pass through;
    every other value has ':'/quotes removed and whitespace collapsed so injected
    qualifier tokens degrade to plain search terms. Mirrors the JQL escaping above.
    """
    safe: dict[str, str] = {}
    for key, value in tmpl_ctx.items():
        if key in _GH_STRUCTURAL_KEYS:
            safe[key] = value
        else:
            safe[key] = " ".join(_GH_QUALIFIER_CHARS.sub(" ", value).split())
    return safe
