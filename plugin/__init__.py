"""hermes-bug-vision-ticket — screenshot -> structured tracker ticket.

A Hermes Agent tool plugin. It registers one tool, ``report_bug_from_screenshot``,
which turns a UI bug screenshot into a structured ticket in Jira, Linear, or
GitHub Issues.

The full pipeline (validate -> vision -> map -> dedup -> create) is wired across
later phases; this module is the discovery/registration entry point the host
calls via ``register(ctx)``.
"""

from __future__ import annotations

import json
from typing import Any, Dict

TOOLSET = "bug_vision_ticket"
TOOL_NAME = "report_bug_from_screenshot"


def _json(obj: Any) -> str:
    """Serialize a handler result to the JSON string Hermes tool handlers must return."""
    return json.dumps(obj, ensure_ascii=False)


def _error(error: str, remediation: str, **extra: Any) -> str:
    """Build the canonical structured-error JSON string."""
    return _json({"success": False, "error": error, "remediation": remediation, **extra})


def handle_report_bug(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Handle ``report_bug_from_screenshot``.

    Skeleton: the real pipeline lands in a later phase. Returns a structured
    not-implemented stub so the contract (JSON string, success flag, remediation)
    is correct from day one.
    """
    return _error(
        "not_implemented",
        "The bug-vision-ticket pipeline is not wired yet; this is the scaffold phase.",
    )


def register(ctx) -> None:
    """Entry point the Hermes PluginManager calls once at load.

    ``ctx`` is a ``hermes_cli.plugins.PluginContext``. The tool ``description``
    is read from inside ``TOOL_SCHEMA`` (the Hermes registry falls back to
    ``schema["description"]``), so it is not passed separately.
    """
    from .schemas import TOOL_SCHEMA

    ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET,
        schema=TOOL_SCHEMA,
        handler=handle_report_bug,
        emoji="🐞",
    )
