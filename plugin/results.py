"""Success-result presenters for the bug-vision-ticket tool.

These build the user-facing JSON *shapes* the handler returns on the happy paths
(dedup / preview / created). Keeping them out of ``__init__`` leaves the
orchestrator focused on wiring + the pipeline + the approval gate, and makes the
output shapes independently testable. Failure shapes live with ``BugTicketError``
(``errors.py``); these are the success counterparts. Each returns a plain dict;
the orchestrator serializes it to the JSON string Hermes handlers must return.
"""

from __future__ import annotations

from typing import Any

from .schemas import BugReport, CreatedIssue

_SUMMARY_MAX = 160


def _short(text: str | None) -> str:
    text = (text or "").strip()
    return text if len(text) <= _SUMMARY_MAX else text[: _SUMMARY_MAX - 1] + "…"


def deduped_result(target: str, ticket_url: str, title: str) -> dict[str, Any]:
    return {
        "success": True,
        "deduped": True,
        "target": target,
        "ticket_url": ticket_url,
        "title": title,
        "message": "An open ticket with this title already exists; "
                   "no duplicate was created.",
    }


def preview_result(target: str, project: str, title: str, report: BugReport) -> dict[str, Any]:
    return {
        "success": True,
        "preview": True,
        "requires_confirmation": True,
        "target": target,
        "project": project,
        "title": title,
        "severity": report.get("severity"),
        "summary": _short(report.get("summary")),
        "message": "Preview only — no ticket was created. Re-invoke with "
                   "confirm=true to file it.",
    }


def created_result(target: str, created: CreatedIssue, title: str, report: BugReport) -> dict[str, Any]:
    return {
        "success": True,
        "created": True,
        "target": target,
        "ticket_url": created.get("url", ""),
        "ticket_id": created.get("id", ""),
        "title": title,
        "summary": _short(report.get("summary")),
    }
