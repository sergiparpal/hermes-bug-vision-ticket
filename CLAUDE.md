# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A single [Hermes Agent](https://github.com/NousResearch/hermes-agent) tool plugin that turns a bug **screenshot** into a structured **ticket** in Jira, Linear, or GitHub Issues, via one tool: `report_bug_from_screenshot`.

- **`plugin/`** is the entire deliverable — `plugin.yaml` (manifest) + the Python package + `tests/`. Edit only here.
- **`hermes-agent/`** is a **gitignored shallow clone of the host runtime**, present only so (a) `hermes_cli` / `tools` / `agent` are importable in tests and (b) `scripts/run_tests.sh` (the CI-parity runner) exists. **It is not this plugin's source — never edit it, never commit it.** The plugin is symlinked into `hermes-agent/dev-plugins/hermes-bug-vision-ticket` so the runner can find it.
- **`DECISIONS.md`** is the source of truth for the **host plugin contract**. It is pinned to Hermes commit `75cd420b3ba1b83185020c6d4506d7cc53b12e2b` and records, with file:line citations, every place the original plan diverged from how the host actually behaves. **Read it before changing anything that touches the host API** (tool/hook registration, the LLM call, plugin loading) — several "obvious" assumptions are wrong and the corrections are load-bearing (see Host contract below).

## Commands

All commands run from the host clone (it owns the venv + runner):

```bash
# One-time setup (clone host, build venv, symlink the plugin in):
git clone --depth 1 https://github.com/NousResearch/hermes-agent.git
( cd hermes-agent && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" )
mkdir -p hermes-agent/dev-plugins
ln -sfn ../../plugin hermes-agent/dev-plugins/hermes-bug-vision-ticket

# Full suite (CI-parity: per-file subprocess isolation, hermetic `env -i`, 30s/test cap):
( cd hermes-agent && bash scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/ )

# Single file:
( cd hermes-agent && bash scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/test_mapping.py )

# Single test (everything after `--` is passed through to per-file pytest):
( cd hermes-agent && bash scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/test_handler.py -- -k test_happy_create )

# Lint — ruff PLW1514 is the ONE enforced rule (see Invariants):
( cd hermes-agent && .venv/bin/python -m ruff check --select PLW1514 --preview dev-plugins/hermes-bug-vision-ticket/ )
```

The suite is fully hermetic — the LLM and all HTTP are mocked, so **no provider, network, or credentials are needed** and tests run in well under a second.

## Architecture

One tool, one pipeline. `_run_pipeline` in `plugin/__init__.py` is the orchestrator; it calls a chain of single-responsibility modules in a deliberate order — **cheap/local checks and all credential validation happen before any side effect**:

```
image_path → vision.resolve_image (validate path, no I/O of contents)
           → config.load_config + resolve_target_name        (read ~/.hermes/bug-tickets.yaml)
           → clients.make_client                             (reads + validates env credentials)
           → vision.extract_bug_report                       (the ONE LLM call)
           → mapping.to_payload                              (pure: BugReport → tracker payload)
           → mapping.build_dedup → client.find_duplicate     (idempotency: return existing, never duplicate)
           → preview (confirm absent/false)  OR  client.create_issue (confirm=true)
```

| Module | Responsibility |
|--------|----------------|
| `__init__.py` | `register(ctx)` entry point, the `_run_pipeline` orchestrator, and the `pre_tool_call` approval hook. The only module that talks to `ctx`. |
| `schemas.py` | **Single source of truth** for `SUPPORTED_TARGETS`, `SEVERITY_LEVELS`, `CONFIDENCE_LEVELS`, the tool schema, and the two BugReport schemas. No imports, no side effects. |
| `config.py` | Load/validate `<HERMES_HOME>/bug-tickets.yaml`, `${VAR}` env expansion, target resolution. |
| `vision.py` | Screenshot → validated, normalized `BugReport`. The only module that calls the host LLM. No tracker logic. |
| `mapping.py` | **Pure** functions: `BugReport` + target config → tracker-specific create payload + dedup descriptor. No network, env, or I/O — this is the load-bearing 90% and is fully unit-tested in isolation. |
| `clients.py` | One REST/GraphQL client per tracker (`find_duplicate` + `create_issue`), HTTP error→`BugTicketError` mapping. The only module that does network I/O. |
| `errors.py` | `BugTicketError(error, remediation, **extra)` — the one structured-error type (the `error` code becomes the JSON `error` key). |

The split between `mapping.py` (pure, builds the payload) and `clients.py` (impure, sends it) is intentional: payload construction is the hard, testable part and stays free of I/O.

## Host contract (verified against `hermes-agent` @ `75cd420…`; full details in DECISIONS.md)

These are the host-API facts the code depends on. They are non-obvious and were wrong in the original plan:

- **Tool registration:** `ctx.register_tool(name=, toolset=, schema=, handler=, emoji=)`. The tool **description lives *inside* the schema dict** (`schema["description"]`), not as a separate arg. The handler **must return a JSON string**; if it raises, the host catches it (`tools/registry.py` `dispatch`, plus a second catch in `agent/tool_executor.py`) and hands the model a generic `{"error": …}` — so raising doesn't crash the agent, it just **loses the structured `remediation`**. That is why `_run_pipeline` catches everything and converts it to a `BugTicketError` payload or `internal_error`.
- **Approval gate:** the real blocking hook is **`pre_tool_call`**, which denies by returning `{"action": "block", "message": ...}`. `pre_approval_request` exists but is **observer-only** (return value ignored). The host **swallows hook exceptions**, so the hook must *return* the block directive, never raise.
- **Vision call:** `ctx.llm.complete_structured(...)` is keyword-only; the ask goes in `instructions`, the image is a block in `input` (`{"type":"image","data":<bytes>,"mime_type":...}`), the schema arg is **`json_schema`** (not `schema`). It returns a **`PluginLlmStructuredResult`** (read `.parsed`), not a dict.
- **The host pre-validates LLM output** against whatever `json_schema` you pass and **raises before your code runs.** This drives the two-schema design (see Invariants) — do not "simplify" it away.
- **Vision uses the user's *main* model** (the host calls `call_llm(task=None)`; there is no `auxiliary.vision` fallback), so that model **must be multimodal**. The plugin selects no model.
- **`requires_env` in `plugin.yaml` is informational only** — it does **not** gate loading or tool availability. The plugin always loads; missing per-tracker credentials surface as a structured error **at call time**.
- **Package import name:** the host imports the plugin as `hermes_plugins.hermes_bug_vision_ticket` (slug = name with `-`→`_`), with `__path__` set to the plugin dir, so **relative imports (`from .schemas import …`) work** despite the hyphenated directory name. Multi-module layout is safe.

## Invariants to preserve when editing

- **Structured errors only.** Every user-facing failure is `raise BugTicketError(code, remediation)`; the handler renders it to `{"success": false, "error", "remediation"}`. Anything else is caught and flattened to `internal_error`. Don't return ad-hoc error dicts — raise `BugTicketError` with a stable `error` code.
- **Two-schema split (vision).** The host call passes the **relaxed** `BUG_REPORT_INPUT_SCHEMA` (no enums / no `required` / `additionalProperties: true`) so the host's pre-validation never rejects fixable output; then `vision._normalize()` coerces (e.g. severity `"high"` → `"critical"`) and `vision._validate()` re-checks against the **strict** `BUG_REPORT_SCHEMA` locally. Tightening the input schema re-breaks normalization.
- **Secrets come only from the environment, never from config.** `config.py` env-expansion **refuses** the credential denylist (`JIRA_API_TOKEN`, `LINEAR_API_KEY`, `GITHUB_TOKEN`) so a stray `${TOKEN}` can't leak into a ticket. Tokens are read in `clients.py` only when a tracker is used, and never logged or echoed in errors.
- **Network rules (clients.py):** HTTPS-only (non-`https://` is refused), explicit timeout on every request, bounded retries (2) on timeouts/connection errors and a fixed set of 5xx (`500/502/503/504`) **only — never on 4xx**.
- **`severity_map` is required per target; never guess a priority/label.** An unmapped severity is a `BugTicketError`. `{placeholder}` templates expand only from a fixed key set; an unknown placeholder is an error.
- **Creation is gated two ways:** the handler never POSTs unless `confirm=true` (default = a non-destructive preview), and the `pre_tool_call` hook blocks unconfirmed calls when `require_approval` (default true). Dedup short-circuits before create. Note the gate is **model-mediated** (the block message goes to the agent, which re-invokes with `confirm=true`) — it prevents accidental creation, not a determined prompt-injection.
- **LLM output is untrusted.** A screenshot can carry prompt-injected text; the system prompt tells the model to treat in-image text as data, and extracted strings are re-validated and escaped (e.g. JQL) before reaching any tracker API.

## Adding a tracker

Support for a tracker is spread across four files — touch all of them:

1. `schemas.py` — add the name to `SUPPORTED_TARGETS` (drives the tool enum, config validation, and `mapping.to_payload`'s guard — but **not** the client factory, which is its own `_CLIENTS` dict; see step 3).
2. `mapping.py` — add a `_<target>_payload` builder, register it in `_BUILDERS`, add the project-field name in `_resolve_project`, and add a branch in `build_dedup`.
3. `clients.py` — add a client class with `find_duplicate(dedup)` and `create_issue(project, payload)`, register it in `_CLIENTS`.
4. `plugin.yaml` — add the tracker's `requires_env` entries (informational) and document its `severity_map` in `plugin/README.md`.

## Tests

Tests live **outside** `hermes-agent/tests/`, so the host's autouse fixtures (HERMES_HOME isolation, registry reset) don't apply — `plugin/tests/conftest.py` re-implements them: it bootstraps the plugin as `hermes_plugins.hermes_bug_vision_ticket`, isolates `HERMES_HOME` per test, and cleans the process-global `tools.registry` afterward. Tests mock the LLM by monkeypatching `vision.extract_bug_report` and mock HTTP by monkeypatching `clients.requests.request`; `test_register.py` exercises real plugin discovery via `PluginManager().discover_and_load()`.

Development is local-git-only (no remote push), historically one commit per implementation phase.
