# hermes-bug-vision-ticket

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) tool plugin that
turns a **bug screenshot** into a **structured ticket** in **Jira**, **Linear**,
or **GitHub Issues** — via a single tool, `report_bug_from_screenshot`.

The agent looks at a screenshot of broken/unexpected UI, extracts a normalized
bug report (title, severity, steps, expected/actual), maps it to the conventions
of your tracker, checks for an existing duplicate, and (after you confirm) files
the ticket — returning the ticket URL.

---

## The tool: `report_bug_from_screenshot`

| Param | Type | Required | Meaning |
|-------|------|----------|---------|
| `image_path` | string | ✅ | Absolute path to the screenshot (`.png/.jpg/.jpeg/.gif/.webp`). |
| `target` | enum `jira`\|`linear`\|`github_issues` | — | Which tracker. Defaults to `default_target` in your config. |
| `project` | string | — | Project key (Jira), team id (Linear), or `owner/repo` (GitHub), overriding the config default. |
| `confirm` | boolean | — | `true` to actually create the ticket. Omitted/`false` returns a **preview** and creates nothing (the safe default). |

**Returns** (a JSON string — bounded to a URL + short summary, never the whole body):

- **Preview** (`confirm` false): `{"success": true, "preview": true, "requires_confirmation": true, "title", "severity", "target", "project", "summary", "message"}`
- **Created** (`confirm` true): `{"success": true, "created": true, "ticket_url", "ticket_id", "title", "summary", "target"}`
- **Deduped** (an open ticket with this title already exists): `{"success": true, "deduped": true, "ticket_url", "title", "target", "message"}`
- **Error**: `{"success": false, "error": "<code>", "remediation": "<how to fix>"}`

### How it works

```
image_path → validate → load config → resolve target + credentials
           → vision (one LLM call) → map to tracker payload
           → find_duplicate → preview  OR  create_issue → ticket URL
```

> **Vision model requirement.** The plugin asks the host LLM
> (`ctx.llm.complete_structured`) to read the screenshot and does **not** select
> a model. Hermes routes this to the user's **active model**, which must be
> **multimodal** for image input to work. (At the pinned Hermes commit there is
> no automatic fallback to a dedicated `auxiliary.vision` model for plugin LLM
> calls.)

---

## Configuration — `~/.hermes/bug-tickets.yaml`

Secrets are **never** stored here; tokens come from environment variables.
String values may reference env vars with `${VAR}` (expanded at load) — use this
only for **non-secret structure** (e.g. a base URL). A `${VAR}` whose name is one of
the tracker credentials *or* simply looks like a secret (contains `TOKEN`, `SECRET`,
`PASSWORD`/`PASSWD`, `CREDENTIAL`, or a `*_KEY` form like `API_KEY`/`ACCESS_KEY`/`PRIVATE_KEY`)
expands to an empty string, so a secret can never be copied into a ticket payload.

```yaml
default_target: github_issues   # used when the tool is called without target=
require_approval: true          # gate creation behind confirm=true (see Approval gate)

targets:
  jira:
    base_url: ${JIRA_BASE_URL}            # https://your-org.atlassian.net
    project_key: ENG
    issue_type: Bug
    labels: [from-screenshot]             # optional, always-added labels
    severity_map:                         # normalized severity -> Jira fields (REQUIRED)
      blocker:  { priority: { name: Highest } }
      critical: { priority: { name: High } }
      major:    { priority: { name: Medium } }
      minor:    { priority: { name: Low } }
      trivial:  { priority: { name: Lowest } }
    custom_fields:                         # arbitrary customfield_XXXXX; {placeholders} pull from the bug report
      customfield_10010: "{component_hint}"
    dedup:
      enabled: true
      jql_template: >
        project = {project_key} AND summary ~ "{title}" AND statusCategory != Done

  linear:
    team_id: <team-uuid>                  # Linear team UUID
    severity_map:
      blocker:  { priority: 1 }
      critical: { priority: 1 }
      major:    { priority: 2 }
      minor:    { priority: 3 }
      trivial:  { priority: 4 }
    label_ids: [<label-uuid>]             # optional Linear label UUIDs
    dedup: { enabled: true }              # matches an existing open issue by title

  github_issues:
    repo: your-org/your-repo
    default_labels: [bug, from-screenshot]
    severity_map:
      blocker:  { labels: [severity:blocker] }
      critical: { labels: [severity:critical] }
      major:    { labels: [severity:major] }
      minor:    { labels: [severity:minor] }
      trivial:  { labels: [severity:trivial] }
    dedup:
      enabled: true
      search_template: 'repo:{repo} is:issue is:open in:title {title}'
```

- **`severity_map`** is required per target — an unmapped severity is a structured
  error (the plugin never guesses a priority/label). A `severity_map` (or Jira
  `custom_fields`) entry may **not** set a core field the builder owns (Jira
  `project`/`issuetype`/`summary`/`description`; Linear `teamId`/`title`/`description`)
  — a colliding key is a structured `reserved_field_override` error, not a silent
  overwrite.
- **`{placeholder}`** templates (in `custom_fields`, `jql_template`,
  `search_template`) expand from the bug report: `title`, `summary`, `severity`,
  `confidence`, `component_hint`, `expected_behavior`, `actual_behavior`, plus
  `project` / `project_key` / `team_id` / `repo`. An unknown placeholder is an error.
  In GitHub `search_template`, untrusted free-text is sanitized so it cannot inject
  search qualifiers. In a Jira `jql_template`, every `{placeholder}` **must sit
  inside a double-quoted JQL literal** (e.g. `summary ~ "{title}"`, as in the
  example above): interpolated values are escaped for a quoted position (quotes and
  backslashes escaped, control chars/newlines stripped), so an **unquoted**
  placeholder would not be safe against injection.
- **`dedup`** is checked before creating; if a match is found (for GitHub/Linear,
  only when the existing issue's **title matches**) the existing ticket URL is
  returned and nothing new is created (idempotent re-runs).
- **Project identifiers are validated:** a Jira `project_key` must be
  `^[A-Za-z][A-Za-z0-9_]*$` and a GitHub `repo` must be a well-formed `owner/name`.
- The filed ticket body includes the model's **observed UI elements** and the
  **text read from the screenshot** (clearly labelled as untrusted, as-read data).

### Required environment variables (per tracker)

Configure only the tracker(s) you use. Missing credentials are reported as a
structured `remediation` error at call time (the plugin still loads).

| Tracker | Variables |
|---------|-----------|
| Jira | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` |
| Linear | `LINEAR_API_KEY` |
| GitHub Issues | `GITHUB_TOKEN` (scope: `repo`/`issues`) |

---

## Approval gate

Creating a ticket is a side effect, so it is gated two ways:

1. **`confirm` flag** — the handler never POSTs unless `confirm=true`. The default
   call returns a non-destructive **preview** of the proposed ticket. `confirm` is
   interpreted strictly (only an explicit affirmative — boolean `true`, or the
   strings `"true"`/`"yes"`/`"1"` — confirms), so a stringified `"false"` can't
   slip the gate open since the host does not coerce tool args to the schema type.
2. **`pre_tool_call` hook** — when `require_approval: true` (the default), an
   unconfirmed call is **blocked** with a message telling the operator to
   re-invoke with `confirm=true`. Set `require_approval: false` to rely on the
   `confirm` flag alone.

(`pre_tool_call` returning `{"action":"block", ...}` is the real blocking hook in
Hermes; the `pre_approval_request` hook is observer-only and cannot deny.)

> **The gate is model-mediated, not a hard human-in-the-loop.** The block message
> is delivered to the agent, which re-invokes with `confirm=true`; Hermes provides
> no human-approval surface for arbitrary plugin tools. So `confirm=true` reflects
> the agent's decision. **Review the preview before instructing the agent to
> confirm.** The gate prevents *accidental/implicit* creation; it does not stop a
> determined prompt-injection that also sets `confirm=true`. The default preview
> and idempotent dedup are the practical safety net.

---

## Install

```bash
# 1. Make the plugin discoverable (copy or symlink into your Hermes home).
ln -s "$(pwd)/plugin" ~/.hermes/plugins/hermes-bug-vision-ticket

# 2. Enable it (plugins are opt-in) in ~/.hermes/config.yaml:
#    plugins:
#      enabled: [hermes-bug-vision-ticket]

# 3. Create ~/.hermes/bug-tickets.yaml (see Configuration) and export the env
#    vars for your tracker.
```

---

## Privilege surface

This plugin runs with the **full privileges of the agent** (Hermes plugins are
not sandboxed). It can:

- **Read one local file** — the screenshot at `image_path`. The path is resolved
  with `os.path.realpath` and validated (must exist, be a regular file, have a
  supported image extension, and be ≤ 15 MiB) before it is read as bytes. The read
  itself opens the file non-blocking and re-confirms it is a regular file (so a path
  swapped to a FIFO/device after validation can neither block nor be streamed).
- **Send the image to the active LLM** — via the host's `ctx.llm`. The screenshot
  bytes are transmitted to whatever model/provider the user has configured.
- **Make authenticated HTTPS writes to your issue tracker** — it reads tracker
  tokens from environment variables (only when a tracker is used) and creates
  issues. Every request is HTTPS-only with an explicit timeout and bounded retries
  (on timeouts, connection errors, `500/502/503/504`, and rate limits — never on an
  ordinary 4xx). **Redirects are not followed**, and a `base_url` resolving to a
  loopback/link-local IP literal (incl. the cloud-metadata endpoint) is refused, so
  an authenticated request can't be bounced to an internal host. Tokens are never
  logged or echoed in errors.
- It does **not** write any files, run any subprocess, or read any other
  credentials. The config file is read-only (the plugin never creates it).

LLM output is treated as **untrusted**: the extracted report is re-validated
against a JSON schema before use, and the system prompt instructs the model to
never obey instructions embedded in the screenshot (prompt-injection defense).
Untrusted strings are escaped/neutralized before they reach a tracker: Markdown
metacharacters that form links/image beacons, inline HTML, or code are escaped in
GitHub/Linear issue bodies; JQL and GitHub-search values are escaped/sanitized; and
any tracker-returned message echoed back into an error is scrubbed of control chars
and clearly marked `[untrusted tracker output]`.

---

## Development

Tests run against the real Hermes plugin contract with the LLM and HTTP fully
mocked (no provider, no network, no tokens):

```bash
# from inside the hermes-agent venv:
scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/

# lint (ruff PLW1514 is the one enforced rule):
.venv/bin/python -m ruff check --select PLW1514 --preview dev-plugins/hermes-bug-vision-ticket/
```

### Project layout

One tool, one pipeline. `_run_pipeline` in `__init__.py` orchestrates a chain of
single-responsibility modules:

| Module | Responsibility |
|--------|----------------|
| `__init__.py` | `register(ctx)`, the `_run_pipeline` orchestrator, and the `pre_tool_call` approval hook — the only module that talks to the host `ctx`. |
| `schemas.py` | **Single source of truth:** the per-tracker `TRACKER_SPECS` registry, the tool + BugReport JSON schemas, and the `TypedDict` contracts (`BugReport`, `MappedPayload`, `DedupDescriptor`, `CreatedIssue`) passed between layers. An import leaf. |
| `config.py` | Load/validate `<HERMES_HOME>/bug-tickets.yaml` — `${VAR}` expansion, target resolution. |
| `vision.py` | Screenshot → validated, normalized `BugReport` (the one LLM call). |
| `mapping.py` | **Pure:** `BugReport` + target config → tracker payload + dedup descriptor. No network/env/I/O. |
| `clients.py` | One REST/GraphQL client per tracker (the only network I/O); each satisfies the `TrackerClient` protocol. |
| `results.py` | The success-result JSON shapes (preview / created / deduped). |
| `coerce.py` | `coerce_bool` — the shared, strict bool coercion behind the `confirm`/approval gate. |
| `errors.py` | `BugTicketError` — the one structured-error type. |

`mapping.py` (pure) and `clients.py` (I/O) never import each other — they
communicate only through the `TypedDict` contracts in `schemas.py`, which keeps
payload construction testable in isolation from the network.

### Adding a tracker

Per-tracker facts live in one table (`schemas.TRACKER_SPECS`), so support for a
new tracker is one registry row plus a builder and a client:

1. `schemas.py` — add a `TrackerSpec` row to `TRACKER_SPECS` (`name`, `project_config_key`, `dedup_kind`, `reserved_fields`). This one row drives `SUPPORTED_TARGETS` (and thus the tool enum + config validation), the project-field resolution, the dedup `kind` tag, and the reserved-field guard.
2. `mapping.py` — add a `_<target>_payload` builder (register it in `_BUILDERS`) and a branch in `build_dedup`.
3. `clients.py` — add a client class with `find_duplicate(dedup)` + `create_issue(project, payload)` and register it in `_CLIENTS`.
4. `plugin.yaml` + this README — document the tracker's `requires_env` and its `severity_map`.

See `../DECISIONS.md` for the pinned Hermes commit and the source-verified
contract this plugin is built against.
