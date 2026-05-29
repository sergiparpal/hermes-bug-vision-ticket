"""Load and validate ``<HERMES_HOME>/bug-tickets.yaml``.

The config maps each tracker target to its field/mapping settings. Secrets are
NEVER stored here — tokens come from environment variables at call time. String
values may reference env vars with ``${VAR}`` (expanded at load; unset -> empty).

Profile-aware home resolution: we ask ``hermes_constants.get_hermes_home()``
when available (honoring HERMES_HOME / the active profile), and fall back to the
HERMES_HOME env var or ``~/.hermes`` so the module also works standalone and in
tests. We never hardcode ``~/.hermes`` as the sole source.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict

import yaml

from .errors import BugTicketError
from .schemas import SUPPORTED_TARGETS

CONFIG_FILENAME = "bug-tickets.yaml"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def hermes_home() -> Path:
    """Resolve the Hermes home dir (profile-aware), never hardcoding ~/.hermes alone."""
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME")
        return Path(env) if env else Path.home() / ".hermes"


def config_path() -> Path:
    return hermes_home() / CONFIG_FILENAME


def _expand_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` refs in strings using os.environ (unset -> '')."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def example_config_text() -> str:
    """A copy-paste-ready minimal config, surfaced in remediation + the README."""
    return (
        "# ~/.hermes/bug-tickets.yaml\n"
        "default_target: github_issues\n"
        "require_approval: true        # gate ticket creation behind confirm=true\n"
        "targets:\n"
        "  github_issues:\n"
        "    repo: your-org/your-repo\n"
        "    default_labels: [bug, from-screenshot]\n"
        "    severity_map:\n"
        "      blocker:  { labels: [severity:blocker] }\n"
        "      critical: { labels: [severity:critical] }\n"
        "      major:    { labels: [severity:major] }\n"
        "      minor:    { labels: [severity:minor] }\n"
        "      trivial:  { labels: [severity:trivial] }\n"
        "    dedup:\n"
        "      enabled: true\n"
        "      search_template: 'repo:{repo} is:issue is:open in:title {title}'\n"
    )


def load_config() -> Dict[str, Any]:
    """Read, env-expand, and structurally validate the config. Raises BugTicketError."""
    path = config_path()
    if not path.exists():
        raise BugTicketError(
            "config_missing",
            f"No config at {path}. Create it; minimal example:\n\n{example_config_text()}",
        )

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BugTicketError("config_unreadable", f"Could not read {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise BugTicketError("config_invalid_yaml", f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise BugTicketError(
            "config_invalid",
            f"{path} must be a YAML mapping with a 'targets' section. Example:\n\n{example_config_text()}",
        )

    data = _expand_env(data)

    targets = data.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise BugTicketError(
            "config_no_targets",
            f"{path} must define a non-empty 'targets' mapping. Example:\n\n{example_config_text()}",
        )

    for name in targets:
        if name not in SUPPORTED_TARGETS:
            raise BugTicketError(
                "config_unknown_target",
                f"Unsupported target '{name}' in {path}. Supported: {', '.join(SUPPORTED_TARGETS)}.",
            )
        if not isinstance(targets[name], dict):
            raise BugTicketError(
                "config_invalid_target",
                f"Target '{name}' in {path} must be a mapping of settings.",
            )

    return data


def resolve_target_name(cfg: Dict[str, Any], requested: str | None) -> str:
    """Pick the target: explicit arg > default_target > the sole configured target."""
    targets = cfg["targets"]
    if requested:
        if requested not in targets:
            raise BugTicketError(
                "target_not_configured",
                f"Target '{requested}' is not in your bug-tickets.yaml (have: "
                f"{', '.join(targets)}).",
            )
        return requested

    default = cfg.get("default_target")
    if default:
        if default not in targets:
            raise BugTicketError(
                "default_target_not_configured",
                f"default_target '{default}' is not under targets ({', '.join(targets)}).",
            )
        return default

    if len(targets) == 1:
        return next(iter(targets))

    raise BugTicketError(
        "no_target_selected",
        "Multiple targets configured and no default_target set; pass target= "
        f"(one of: {', '.join(targets)}) or set default_target.",
    )


def require_approval(cfg: Dict[str, Any]) -> bool:
    """Whether ticket creation must be confirmed (default True)."""
    val = cfg.get("require_approval", True)
    return bool(val)
