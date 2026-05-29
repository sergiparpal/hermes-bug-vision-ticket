"""Shared structured-error type for the bug-vision-ticket pipeline.

Every user-facing failure (bad image, missing config, unmapped severity,
invalid credentials, tracker unreachable, ...) is raised as a
``BugTicketError`` carrying a stable ``error`` code and a human ``remediation``
hint. The tool handler catches it and renders the canonical
``{"success": false, "error": ..., "remediation": ...}`` JSON string, so error
shaping lives in exactly one place and every module stays focused.
"""

from __future__ import annotations

from typing import Any, Dict


class BugTicketError(Exception):
    """A pipeline failure that should surface to the model as a structured error."""

    def __init__(self, error: str, remediation: str, **extra: Any) -> None:
        super().__init__(error)
        self.error = error
        self.remediation = remediation
        self.extra = extra

    def to_payload(self) -> Dict[str, Any]:
        return {
            "success": False,
            "error": self.error,
            "remediation": self.remediation,
            **self.extra,
        }
