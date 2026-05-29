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
from typing import Any, Dict, List, Optional

from .errors import BugTicketError
from .schemas import SUPPORTED_TARGETS

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_JIRA_SUMMARY_MAX = 255


# ---------------------------------------------------------------------------
# Template expansion
# ---------------------------------------------------------------------------
def _template_context(bug_report: Dict[str, Any], project: str) -> Dict[str, str]:
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


def _expand_str(template: str, ctx: Dict[str, str]) -> str:
    def repl(m: "re.Match[str]") -> str:
        key = m.group(1)
        if key not in ctx:
            raise BugTicketError(
                "missing_placeholder",
                f"Template references unknown placeholder '{{{key}}}'. Known: "
                f"{', '.join(sorted(ctx))}.",
            )
        return ctx[key]

    return _PLACEHOLDER.sub(repl, template)


def _expand_value(value: Any, ctx: Dict[str, str]) -> Any:
    if isinstance(value, str):
        return _expand_str(value, ctx)
    if isinstance(value, list):
        return [_expand_value(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: _expand_value(v, ctx) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Body composition (shared sections -> ADF or markdown)
# ---------------------------------------------------------------------------
def _sections(bug_report: Dict[str, Any]) -> List[Dict[str, Any]]:
    sections: List[Dict[str, Any]] = []

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

    confidence = bug_report.get("confidence", "medium")
    sections.append({
        "heading": None,
        "kind": "para",
        "content": f"Filed from a screenshot via hermes-bug-vision-ticket (model confidence: {confidence}).",
    })
    return sections


def _render_markdown(bug_report: Dict[str, Any]) -> str:
    out: List[str] = []
    for sec in _sections(bug_report):
        if sec["heading"]:
            out.append(f"## {sec['heading']}")
        if sec["kind"] == "para":
            out.append(sec["content"])
        else:
            out.extend(f"- {item}" for item in sec["content"])
        out.append("")
    return "\n".join(out).strip()


def _render_adf(bug_report: Dict[str, Any]) -> Dict[str, Any]:
    """Minimal Atlassian Document Format doc (Jira Cloud /rest/api/3 description)."""
    content: List[Dict[str, Any]] = []
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
def _resolve_project(target: str, cfg: Dict[str, Any], override: Optional[str]) -> str:
    field = {"jira": "project_key", "linear": "team_id", "github_issues": "repo"}[target]
    value = (override or cfg.get(field) or "").strip()
    if not value:
        raise BugTicketError(
            "missing_project",
            f"No project for target '{target}'. Set '{field}' in bug-tickets.yaml "
            "or pass project=.",
        )
    if target == "github_issues" and "/" not in value:
        raise BugTicketError(
            "invalid_repo",
            f"GitHub repo must be 'owner/name', got '{value}'.",
        )
    return value


def _severity_entry(cfg: Dict[str, Any], severity: str, target: str) -> Dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Per-target payload builders
# ---------------------------------------------------------------------------
def _jira_payload(bug_report, cfg, project, ctx) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "project": {"key": project},
        "issuetype": {"name": cfg.get("issue_type", "Bug")},
        "summary": (bug_report["title"] or "")[:_JIRA_SUMMARY_MAX],
        "description": _render_adf(bug_report),
    }
    labels = cfg.get("labels")
    if labels:
        fields["labels"] = [str(x) for x in labels]

    fields.update(_severity_entry(cfg, bug_report["severity"], "jira"))

    for key, value in (cfg.get("custom_fields") or {}).items():
        fields[key] = _expand_value(value, ctx)

    return {"fields": fields}


def _linear_payload(bug_report, cfg, project, ctx) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "teamId": project,
        "title": bug_report["title"],
        "description": _render_markdown(bug_report),
    }
    payload.update(_severity_entry(cfg, bug_report["severity"], "linear"))

    label_ids = cfg.get("label_ids") or cfg.get("labels")
    if label_ids:
        payload["labelIds"] = [str(x) for x in label_ids]
    return payload


def _github_payload(bug_report, cfg, project, ctx) -> Dict[str, Any]:
    labels: List[str] = [str(x) for x in (cfg.get("default_labels") or [])]
    entry = _severity_entry(cfg, bug_report["severity"], "github_issues")
    for label in entry.get("labels", []) or []:
        if label not in labels:
            labels.append(str(label))

    payload: Dict[str, Any] = {
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
    bug_report: Dict[str, Any],
    target: str,
    target_cfg: Dict[str, Any],
    project: Optional[str] = None,
) -> Dict[str, Any]:
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
    ctx = _template_context(bug_report, resolved)
    create_payload = _BUILDERS[target](bug_report, target_cfg, resolved, ctx)

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
    bug_report: Dict[str, Any],
    target: str,
    target_cfg: Dict[str, Any],
    project: str,
) -> Optional[Dict[str, Any]]:
    """Build the dedup search descriptor for a target, or None if disabled.

    Returns one of:
      {"kind": "jql", "jql": "..."}                 (Jira)
      {"kind": "github_search", "q": "..."}         (GitHub)
      {"kind": "linear", "title": "..."}            (Linear; client matches by title)
    """
    dedup = target_cfg.get("dedup")
    if not isinstance(dedup, dict) or not dedup.get("enabled"):
        return None

    if target == "jira":
        template = dedup.get("jql_template")
        if not template:
            return None
        # Escape double quotes in title so the JQL string literal stays valid.
        ctx = _template_context(bug_report, project)
        ctx["title"] = ctx["title"].replace('"', '\\"')
        return {"kind": "jql", "jql": _expand_str(template, ctx).strip()}

    if target == "github_issues":
        template = dedup.get("search_template")
        if not template:
            return None
        ctx = _template_context(bug_report, project)
        return {"kind": "github_search", "q": _expand_str(template, ctx).strip()}

    if target == "linear":
        return {"kind": "linear", "title": bug_report.get("title", "")}

    return None
