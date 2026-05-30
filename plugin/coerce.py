"""Shared, tolerant boolean coercion.

Two places interpret a possibly-stringy boolean and a naive ``bool()`` would read
the *string* ``"false"`` / ``"no"`` / ``"0"`` as True (any non-empty string is
truthy) — flipping a security gate OPEN. Both route through ``coerce_bool`` so the
logic lives once:

* ``__init__._confirmed`` — the create/approval gate (strict: only an explicit
  affirmative; numeric ``1`` only).
* ``config._as_bool`` — the ``require_approval`` config flag (tolerant: also
  ``on``/``off`` and a caller default; any non-zero number is True).
"""

from __future__ import annotations

from typing import Any

_TRUE_TOKENS = ("true", "yes", "1")
_FALSE_TOKENS = ("false", "no", "0")


def coerce_bool(
    value: Any,
    *,
    default: bool = False,
    true_tokens: tuple[str, ...] = _TRUE_TOKENS,
    false_tokens: tuple[str, ...] = _FALSE_TOKENS,
    strict_numeric: bool = False,
) -> bool:
    """Interpret ``value`` as a bool.

    * ``bool`` -> itself.
    * ``str`` -> case-insensitive, stripped: in ``true_tokens`` -> True, in
      ``false_tokens`` -> False, otherwise ``default``.
    * ``int``/``float`` -> ``== 1`` when ``strict_numeric`` (only 1 is True),
      else ``!= 0``.
    * anything else (incl. None) -> ``default``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in true_tokens:
            return True
        if token in false_tokens:
            return False
        return default
    if isinstance(value, (int, float)):
        return value == 1 if strict_numeric else value != 0
    return default
