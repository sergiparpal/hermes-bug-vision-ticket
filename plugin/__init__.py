"""hermes-bug-vision-ticket — screenshot -> structured tracker ticket.

A Hermes Agent tool plugin. It registers one tool, ``report_bug_from_screenshot``,
that turns a UI bug screenshot into a structured ticket in Jira, Linear, or
GitHub Issues, and a ``pre_tool_call`` approval hook that gates creation.

Pipeline (handle): validate image -> load config -> resolve target + creds ->
vision extract -> map to tracker payload -> dedup -> preview OR create.

Approval gate (Q4 = yes): ticket creation is a side effect, so it is gated two
ways. (1) The handler never POSTs unless ``confirm=true`` — the default is a
non-destructive preview the operator can review. (2) A ``pre_tool_call`` hook
blocks unconfirmed calls when ``require_approval`` is on in the config (the
verified, real blocking hook; ``pre_approval_request`` is observer-only and
cannot deny). Idempotent dedup returns an existing ticket without creating a
duplicate.

Note: this gate is model-mediated, not a hardware human-in-the-loop. The block
message goes to the agent, which re-invokes with ``confirm=true``; Hermes has no
human-approval surface for arbitrary plugin tools. So ``confirm=true`` reflects
the agent's decision — review the preview before instructing the agent to
confirm. The value is preventing accidental/implicit creation, not defeating a
determined prompt-injection that also sets ``confirm=true``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

TOOLSET = "bug_vision_ticket"
TOOL_NAME = "report_bug_from_screenshot"


def _json(obj: Any) -> str:
    """Serialize a handler result to the JSON string Hermes tool handlers must return."""
    return json.dumps(obj, ensure_ascii=False)


def _confirmed(args: dict[str, Any]) -> bool:
    """Strictly interpret the ``confirm`` flag — the creation/approval gate.

    The host does NOT validate or coerce tool-call arguments against the tool's
    parameter schema (``agent/tool_executor.py`` does a bare ``json.loads`` and
    only checks the result is a dict), so whatever the model emits arrives here
    verbatim. A naive ``bool(args.get("confirm"))`` would treat the *string*
    ``"false"`` / ``"no"`` / ``"0"`` as True (any non-empty string is truthy),
    failing BOTH the preview default and the pre_tool_call approval gate OPEN.
    Only an explicit affirmative confirms; anything negative/ambiguous does not.
    Both the handler and the hook route through this single helper (and the shared
    ``coerce_bool``) so they can never diverge.
    """
    # Lazy import: __init__.py must keep every relative import inside a function so
    # the module body stays importable in a non-package context (pytest collects
    # this file directly; the host probes it). Mirrors the lazy imports below.
    from .coerce import coerce_bool

    return coerce_bool(args.get("confirm"), default=False, strict_numeric=True)


def _run_pipeline(ctx, args: dict[str, Any]) -> str:
    """Run the full report-a-bug pipeline and return a bounded JSON string.

    ``ctx`` is the PluginContext (for ``ctx.llm``). Kept module-level (rather
    than a closure) so it is directly unit-testable with a fake ctx.
    """
    from . import clients, config, mapping, results, vision
    from .errors import BugTicketError

    args = args if isinstance(args, dict) else {}
    confirm = _confirmed(args)

    try:
        # 1. Cheap, direct input validation first (no LLM, no network). Keep the
        #    resolved (path, mime) so extract_bug_report need not re-stat the file.
        resolved = vision.resolve_image(args.get("image_path"))

        # 2. Config + target + credentials — all before any side effect.
        cfg = config.load_config()
        target = config.resolve_target_name(cfg, args.get("target"))
        target_cfg = cfg["targets"][target]
        client = clients.make_client(target, target_cfg)  # validates credentials

        # 3. Vision extraction (the one LLM call).
        report = vision.extract_bug_report(ctx, args["image_path"], resolved=resolved)

        # 4. Map to a tracker payload (pure).
        mapped = mapping.to_payload(report, target, target_cfg, args.get("project"))
        project = mapped["project"]
        title = mapped["title"]

        # 5. Idempotency: never create a duplicate.
        dedup = mapping.build_dedup(report, target, target_cfg, project)
        if dedup is not None:
            existing = client.find_duplicate(dedup)
            if existing:
                return _json(results.deduped_result(target, existing, title))

        # 6. Preview (safe default) vs. create.
        if not confirm:
            return _json(results.preview_result(target, project, title, report))

        created = client.create_issue(project, mapped["create_payload"])
        return _json(results.created_result(target, created, title, report))

    except BugTicketError as exc:
        return _json(exc.to_payload())
    except Exception:  # noqa: BLE001 — tool handlers must not crash the agent loop
        logger.exception("report_bug_from_screenshot failed unexpectedly")
        return _json(BugTicketError(
            "internal_error",
            "The plugin hit an unexpected error. Check the image path and "
            "~/.hermes/bug-tickets.yaml, then retry.",
        ).to_payload())


def _approval_required() -> bool:
    """Whether unconfirmed creation should be blocked. Defaults to True if config is unusable."""
    from . import config
    from .errors import BugTicketError

    try:
        return config.require_approval(config.load_config())
    except BugTicketError:
        return True  # no/invalid config -> err on the safe side and require approval


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_kwargs: Any) -> dict[str, str] | None:
    """Approval gate for ticket creation.

    Returns None to allow, or {"action":"block","message":...} to deny. Only
    acts on our tool; an unconfirmed call is blocked when approval is required so
    the operator must opt in with confirm=true. (Raising would NOT deny — the
    host swallows hook exceptions — so we always return the block directive.)
    """
    if tool_name != TOOL_NAME:
        return None
    args = args if isinstance(args, dict) else {}
    if _confirmed(args):
        return None  # explicit confirmation present -> allow creation
    if not _approval_required():
        return None  # approval disabled in config -> allow (handler still previews)

    target = args.get("target") or "the configured tracker"
    return {
        "action": "block",
        "message": (
            f"Filing a bug ticket creates a new issue in {target}. This requires "
            "approval. Re-invoke report_bug_from_screenshot with confirm=true to "
            "proceed (or call it with confirm=false for a no-op preview)."
        ),
    }


def register(ctx) -> None:
    """Entry point the Hermes PluginManager calls once at load."""
    from .schemas import TOOL_SCHEMA

    def handler(args: dict[str, Any], **_kwargs: Any) -> str:
        return _run_pipeline(ctx, args)

    ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET,
        schema=TOOL_SCHEMA,
        handler=handler,
        emoji="🐞",
    )
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
