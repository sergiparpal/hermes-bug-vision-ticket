# Implementation Plan — `hermes-bug-vision-ticket` (Hermes Agent plugin)

> **Audience:** an autonomous coding agent (Claude Code CLI).
> **Goal:** ship a Hermes Agent plugin that turns a bug **screenshot** into a
> **structured ticket** in Jira / Linear / GitHub Issues, via a single tool
> `report_bug_from_screenshot`.
> **Effort budget:** 6–10 hours of agent work, split into 6 gated phases.

---

## 0. How to read and execute this plan

This plan is written to run **end-to-end without human checkpoints between
phases**. There is exactly **one** interactive moment — the short interview in
**Phase 1** — and even that has documented defaults so the run can proceed if
the operator answers "use defaults".

### Execution protocol (apply to every phase)

1. Do the phase's work in order.
2. Run the phase's **gate command** (always a `scripts/run_tests.sh` invocation).
   The gate passes only when it exits `0`.
3. On green: `git add -A && git commit -m "phase N: <summary>"`, then **advance
   to the next phase automatically. Do not stop to ask for review.**
4. On red: fix within the same phase and re-run the gate (reasonable number of
   attempts). **Never advance on red.**
5. If a genuinely blocking ambiguity appears that this plan did **not** cover,
   ask the operator **one** concise question, then continue. Otherwise pick the
   documented default, **append a one-line note to `DECISIONS.md`**, and keep
   going. `DECISIONS.md` exists so the operator can review autonomous choices
   *after* the run instead of blocking it *during* the run.

### Claude Code operating notes

- **Read `AGENTS.md` at the repo root before writing any code.** It is the hard
  rule source for this project (test execution, style). Re-read it before the
  final phase. *(Critical path: `AGENTS.md`.)*
- **Never run bare `pytest`.** Use `scripts/run_tests.sh` (it enforces CI parity:
  unsets credential env vars, `TZ=UTC`, `LANG=C.UTF-8`, 4 xdist workers). Bare
  pytest will give false greens/reds versus CI.
- **Do not run `claude init`** if a `CLAUDE.md` or `AGENTS.md` already exists in
  the repo — it can clobber existing project context. If you want a focused
  constraints file for this work, create `dev-plugins/hermes-bug-vision-ticket/CLAUDE.md`
  by hand (see Phase 2 checklist).
- **Effort:** use a capable model. Phases 4 and 5 (tracker mapping + REST /
  idempotency) are where mistakes cluster — prefix those prompts with
  `think hard` (or `ultrathink` for the mapping schema design).
- **Source-first discipline:** Hermes is under very active development and parts
  of the plugin contract are only documented in source, not in the public docs.
  When this plan says "verify against source", actually open the file named and
  encode what you find in a code comment. Do **not** fabricate API shapes.

---

## 1. Phase 1 — Operator interview (the only interactive step)

Ask the operator the following as **one batched survey**. If they reply with
"use defaults" (or skip any item), apply the default shown and record it in
`DECISIONS.md`. Then proceed autonomously through all remaining phases.

| # | Question | Default if unanswered |
|---|---|---|
| 1 | Which trackers should v1 support? (`jira`, `linear`, `github_issues`, or all) | **All three** |
| 2 | Do you have working credentials / a sandbox for a **live** smoke test, or keep everything **mocked**? | **Mocked only** (live smoke skipped) |
| 3 | `author` string for `plugin.yaml`, and a git remote slug if you want one (`you/hermes-bug-vision-ticket`) | `author: "unknown"`, **local git only**, no remote |
| 4 | Should ticket creation require an explicit **approval** before the POST? | **Yes** (wire `pre_approval_request`) |
| 5 | Path to an existing `hermes-agent` clone, or clone fresh? | **Clone fresh** into `./hermes-agent` |

> The defaults are chosen so the run **never hard-blocks**: missing credentials →
> tests stay mocked and still pass; no remote → publish step is skipped; approval
> on → safer side-effects. Everything is buildable and testable with zero secrets.

**Gate for Phase 1:** none (this phase produces `DECISIONS.md` only).

---

## 2. Technical requirements (Hermes Agent plugin contract)

This section is the reference the agent must satisfy. All facts are taken from
the project's plugin contract; **verify the items marked ⚠ against source before
relying on them**, because they were documented from code inspection.

### 2.1 Environment

- **OS:** Linux/macOS, or **WSL2 on Windows** (native Windows support is beta —
  prefer WSL2).
- **Python:** the version required by `hermes-agent` itself. **Do not assume a
  number** — read `pyproject.toml` / `setup.cfg` in the clone and match it.
- **The `hermes-agent` repo must be cloned and set up**, because:
  - the plugin's tests import `from hermes_cli.plugins import PluginManager`, so
    `hermes_cli` must be importable;
  - `scripts/run_tests.sh` (the required CI-parity test runner) lives there.

```bash
# Setup (Phase 3 will run this)
git clone https://github.com/NousResearch/hermes-agent.git
cd hermes-agent
./setup-hermes.sh          # creates the venv / installs hermes_cli
```

After setup, **record the exact commit/tag** you are building against in
`DECISIONS.md` (Hermes APIs move fast — pin what you built on).

### 2.2 Plugin anatomy

A plugin is a directory containing `plugin.yaml` + an `__init__.py` that exposes
a `register(ctx)` function. Multi-module layout (what this plugin uses):

```
hermes-bug-vision-ticket/
├── plugin.yaml
├── __init__.py          # register(ctx): wires the tool
├── schemas.py           # BugReport JSON schema + tool schema
├── vision.py            # screenshot -> BugReport (ctx.llm)
├── mapping.py           # BugReport + config -> tracker payload  (the hard part)
├── clients.py           # one REST client per tracker
├── config.py            # load/validate ~/.hermes/bug-tickets.yaml
├── tests/
│   ├── test_register.py
│   ├── test_vision.py
│   ├── test_mapping.py
│   ├── test_clients.py
│   └── test_handler.py
├── CLAUDE.md            # focused constraints for this work (optional)
└── README.md
```

### 2.3 `PluginContext` API surface (the only APIs you may call)

| API | Signature | Notes |
|---|---|---|
| `ctx.register_tool` | `(name, toolset, schema, handler, check_fn=None)` | **`description` is NOT a kwarg** — it lives inside `schema["description"]` (OpenAI function-calling style). `check_fn` returning `False` hides the tool (use it for optional deps). |
| `ctx.register_hook` | `(hook_name, callback)` | For `pre_approval_request` (approval gate) if Q4 = yes. |
| `ctx.llm` | property → `.complete(...)`, `.complete_structured(...)`, `.acomplete(...)`, `.acomplete_structured(...)` | Borrows the active user's provider/model/auth. No manual client wiring. |
| `ctx.dispatch_tool` | `(name, arguments)` | Only if you ever compose over another tool — respects approvals/redaction/budgets. Not needed for v1. |

⚠ **Do not invent kwargs.** `is_async` / `emoji` may exist in code but are
unverified — open an issue before using them.

### 2.4 Multimodal / vision call (verified against `agent/plugin_llm.py`)

- The structured signature is
  `complete_structured(*, instructions, input: Sequence[PluginLlmInput], ...)`.
- The **image block** canonical form is
  `{"type": "image", "data": b"...", "mime_type": "image/png"}`
  (a URL form `{"type": "image", "url": "https://..."}` also exists).
- **Routing is automatic and transparent:** if the user's active text model is
  text-only, the host falls back to the configured `auxiliary.vision` model. The
  plugin **decides nothing** — no model selection, no internal tool calls.
- **⚠ Verify the text-input block shape first.** The image block is confirmed;
  the exact `PluginLlmTextInput` key (`text` vs `data`) and whether the textual
  ask belongs in `instructions` or as an `input` block must be confirmed against
  `agent/plugin_llm.py` and the official example before coding `vision.py`.
- **Reference implementation to read:** the example repo
  `hermes-example-plugins/plugin-llm-example` does sync structured extraction
  with image input — mirror its call shape.

### 2.5 `plugin.yaml` manifest rules

The canonical manifest dataclass is `PluginManifest` in `hermes_cli/plugins.py`.
Its fields are a **closed set**: `name`, `version`, `description`, `author`,
`requires_env`, `provides_tools`, `provides_hooks`, `source`, `path`, `kind`,
`key`. **Anything else is invalid.** In particular:

- ❌ `requires_hermes_version` **does not exist** (grep returns nothing). If you
  must pin the Hermes version, do it at runtime inside `register(ctx)`:
  `from hermes_cli import __version__` and abort with a clear message if it
  doesn't fit. (`hermes_requires` is a *profile-distribution* field, not a
  plugin field — don't use it here.)
- ❌ A `kind: memory` field is **not** part of the official contract — irrelevant
  here anyway (this is a tool plugin, not a memory provider).
- ✅ `requires_env` is the correct field for required secrets. If a declared var
  is missing, the plugin **auto-disables with a clear message** — which is
  exactly the behaviour we want for tracker tokens.

### 2.6 Handler contract

- Tool **handlers must return a JSON-encoded string** (`json.dumps(...)`), never
  a dict or object.
- Plugin activation is **automatic** once the plugin is discovered — there is no
  `config.yaml` enable flag to set. (`hermes plugins enable` exists for the
  lifecycle UI, but discovery alone surfaces the tool.)

### 2.7 Test contract (mandatory pattern)

```python
# tests/test_register.py
from pathlib import Path
import pytest

@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home

def test_plugin_registers_tool(tmp_path, profile_env):
    from hermes_cli.plugins import PluginManager
    pm = PluginManager()
    pm.discover_and_load_from(Path(__file__).parent.parent)
    tools = pm.get_registered_tools()
    assert "report_bug_from_screenshot" in tools
    assert tools["report_bug_from_screenshot"]["toolset"] == "bug_vision_ticket"
```

Run it (and all gates) via:

```bash
# from inside the hermes-agent venv:
scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/
```

> **Why `scripts/run_tests.sh` and not `pytest`:** CI parity is mandatory per
> `AGENTS.md`. The script unsets credential vars (so any test that *needs* a
> token MUST mock it), pins `TZ=UTC` / `LANG=C.UTF-8`, and uses 4 xdist workers.
> A test that passes under bare pytest but reads ambient credentials will fail in
> CI — the script catches that locally.

### 2.8 Security & tool-design rules (hard requirements)

Plugins run with the **full privileges of the agent** — there is no sandbox. The
only boundary is operator review before install, so the bar is high:

- `os.path.realpath()` the `image_path` before any access check; reject paths
  that escape expected roots.
- **HTTPS + an explicit timeout on every network call.** No exceptions.
- **Redact secrets in logs.** Never log tokens, never echo `.env` / `auth.json`.
- **Validate the LLM's output** before acting on it — a screenshot can carry
  prompt-injected text. Treat extracted strings as untrusted data.
- No `exec` / `eval` of strings. `shlex.quote()` anything that reaches a
  subprocess (none expected here, but enforce it if it appears).
- Do not write outside `$HERMES_HOME` except the explicit config path.
- Structured errors only: `{"success": false, "error": "...", "remediation": "..."}`.
- Tool name `report_bug_from_screenshot` is snake_case and must **not** collide
  with built-ins (`web_search`, `terminal`, `read_file`).
- In the tool `schema["description"]`: say **when to use it** and **what it
  returns**; **do not name tools from other toolsets** (they may be disabled →
  hallucination risk).
- Creating a ticket is a **side-effect** → if Q4 = yes, gate it behind the
  approval system via a `pre_approval_request` hook.
- Keep tool output **bounded** (a returned URL + a short summary; not the whole
  ticket body) so it doesn't blow the prompt cache.
- Tests must cover **network errors and invalid credentials**.
- The README must **document the privilege surface** (what the plugin can touch).

---

## 3. Phase 2 — Scaffold + green skeleton

**Goal:** a discoverable plugin that registers the tool with a valid (but
not-yet-functional) handler, and a passing registration test.

**Read first (source-first):** `hermes_cli/plugins.py` (manifest + PluginManager),
and skim the public **"Build a Hermes Plugin"** guide.

**Do:**

1. Clone + set up `hermes-agent` (per the operator's Q5 answer); record the
   commit in `DECISIONS.md`.
2. Create the directory tree from §2.2 under
   `hermes-agent/dev-plugins/hermes-bug-vision-ticket/` (developing inside the
   clone keeps `hermes_cli` importable and `scripts/run_tests.sh` adjacent;
   you can extract it to its own repo in Phase 7).
3. Write `plugin.yaml` using **only** the allowed manifest fields:

   ```yaml
   name: hermes-bug-vision-ticket
   version: 0.1.0
   description: "Turn a bug screenshot into a structured ticket in Jira/Linear/GitHub Issues."
   author: "<from Q3>"
   provides_tools: [report_bug_from_screenshot]
   provides_hooks: [pre_approval_request]   # omit if Q4 = no
   requires_env:
     # Declare only what the selected trackers (Q1) need. Examples:
     - name: JIRA_BASE_URL
       description: "Base URL of the Jira instance"
       secret: false
     - name: JIRA_EMAIL
       description: "Account email for Jira API auth"
       secret: false
     - name: JIRA_API_TOKEN
       description: "Jira API token"
       url: "https://id.atlassian.com/manage-profile/security/api-tokens"
       secret: true
     # LINEAR_API_KEY, GITHUB_TOKEN as applicable
   ```

   > Note on `requires_env`: declaring a token here means the plugin
   > **auto-disables** if it's absent. If you want the plugin to load even when
   > only *some* trackers are configured, declare only the truly always-required
   > vars here and check the per-tracker ones at call time inside the handler
   > (return a structured `remediation` error instead of failing to load). Record
   > the choice in `DECISIONS.md`.

4. Write `__init__.py` with a minimal but contract-correct `register`:

   ```python
   """hermes-bug-vision-ticket — screenshot -> structured tracker ticket."""
   import json

   def register(ctx):
       from .schemas import TOOL_SCHEMA  # description lives INSIDE this schema

       def handle_report_bug(params, **kwargs):
           # Phase 6 fills this in. Skeleton returns a structured stub.
           return json.dumps({
               "success": False,
               "error": "not_implemented",
               "remediation": "Plugin skeleton only; handler lands in a later phase.",
           })

       ctx.register_tool(
           name="report_bug_from_screenshot",
           toolset="bug_vision_ticket",
           schema=TOOL_SCHEMA,
           handler=handle_report_bug,
       )
   ```

5. Write `schemas.py` with the **tool schema** (description + strictly typed
   params, `required` minimal):

   ```python
   TOOL_SCHEMA = {
       "name": "report_bug_from_screenshot",
       "description": (
           "Analyze a UI bug screenshot and create a structured bug ticket in the "
           "configured tracker. Use when the user supplies a screenshot of broken/"
           "unexpected UI and wants it filed. Returns JSON with the created ticket "
           "URL and a short summary, or a structured error."
       ),
       "parameters": {
           "type": "object",
           "properties": {
               "image_path": {"type": "string", "description": "Absolute path to the screenshot (png/jpg)."},
               "target": {"type": "string", "enum": ["jira", "linear", "github_issues"],
                          "description": "Which tracker to file into. Defaults to bug-tickets.yaml default_target."},
               "project": {"type": "string", "description": "Project/board key or repo slug, overriding config."},
           },
           "required": ["image_path"],
       },
   }
   ```

6. Write `tests/test_register.py` (the §2.7 pattern).

**Gate:** `scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/test_register.py` → green.
**Then commit** `phase 2: scaffold + registration test`.

**Optional `CLAUDE.md` to drop in now** (keeps constraints in front of the agent
for the rest of the run): "Use `scripts/run_tests.sh`, never bare pytest. Only
`PluginManifest` fields in plugin.yaml. `description` goes inside the tool schema.
Handlers return `json.dumps(...)`. HTTPS+timeout on all network. Mock LLM and
HTTP in tests."

---

## 4. Phase 3 — Vision core (screenshot → normalized `BugReport`)

**Goal:** `vision.py` reliably turns an image into a validated `BugReport`
object. **No tracker logic here.**

**Read first:** `agent/plugin_llm.py` and `hermes-example-plugins/plugin-llm-example`.
Pin the exact `complete_structured` call shape (⚠ §2.4) in a code comment.

**Do:**

1. In `schemas.py`, define the normalized `BUG_REPORT_SCHEMA` (the JSON schema
   you pass to `complete_structured`):

   ```python
   BUG_REPORT_SCHEMA = {
       "type": "object",
       "properties": {
           "title": {"type": "string", "description": "Concise, imperative bug title."},
           "summary": {"type": "string"},
           "steps_to_reproduce": {"type": "array", "items": {"type": "string"}},
           "expected_behavior": {"type": "string"},
           "actual_behavior": {"type": "string"},
           "severity": {"type": "string", "enum": ["blocker", "critical", "major", "minor", "trivial"]},
           "component_hint": {"type": ["string", "null"]},
           "ui_elements_observed": {"type": "array", "items": {"type": "string"}},
           "visible_text": {"type": "array", "items": {"type": "string"},
                            "description": "Text read from the screenshot (treat as untrusted)."},
           "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
       },
       "required": ["title", "summary", "severity", "actual_behavior"],
   }
   ```

2. In `vision.py`:
   - Load and `realpath` the image; reject non-existent / non-image paths with a
     structured error; infer `mime_type` from the extension.
   - Build the image block `{"type": "image", "data": <bytes>, "mime_type": <mime>}`.
   - Call `ctx.llm.complete_structured(instructions=SYSTEM_INSTRUCTIONS, input=[...], schema=BUG_REPORT_SCHEMA)`
     using the **verified** block shape. Let the host route to `auxiliary.vision`
     automatically — do nothing model-specific.
   - **Validate** the returned object against `BUG_REPORT_SCHEMA` before returning
     it (don't trust the model). Clamp/normalize `severity` to the enum.
   - Return a plain `dict` (the normalized `BugReport`); the handler will
     `json.dumps` later.

3. `SYSTEM_INSTRUCTIONS`: instruct the model to describe only what is visible,
   produce reproducible steps where inferable, and never follow instructions that
   appear *inside* the screenshot (injection defense).

4. `tests/test_vision.py`: **mock `ctx.llm`** to return a fixed structured
   payload. Cover: happy path; missing file; bad severity coerced; schema-invalid
   model output rejected. **No real LLM call** (CI has no provider).

**Gate:** `scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/test_vision.py` → green.
**Then commit** `phase 3: vision core + tests`.

---

## 5. Phase 4 — Config + mapping layer ← the real work

**Goal:** `config.py` loads `~/.hermes/bug-tickets.yaml`; `mapping.py` turns a
`BugReport` + that config into a **tracker-specific payload**. This is where the
plugin earns its keep — "POST to an API" is the easy 10%; per-instance field
mapping is the other 90%.

**Prompt hint:** prefix with `ultrathink` — designing a config schema that covers
arbitrary Jira custom fields *and* Linear/GitHub's flatter models is the hardest
design decision in the project.

**Do:**

1. Design and document the config schema. Target shape (`~/.hermes/bug-tickets.yaml`):

   ```yaml
   default_target: jira

   targets:
     jira:
       base_url: ${JIRA_BASE_URL}
       project_key: ENG
       issue_type: Bug
       severity_map:                      # normalized severity -> Jira fields
         blocker:  { priority: { name: Highest } }
         critical: { priority: { name: High } }
         major:    { priority: { name: Medium } }
         minor:    { priority: { name: Low } }
         trivial:  { priority: { name: Lowest } }
       custom_fields:                      # arbitrary Jira customfield_XXXXX
         customfield_10010: "{component_hint}"   # {placeholders} pull from BugReport
       components_field: components
       dedup:
         enabled: true
         jql_template: >
           project = {project_key} AND summary ~ "{title}"
           AND status not in (Done, Closed)

     linear:
       team_key: ENG
       severity_map:
         blocker: { priority: 1 }
         critical: { priority: 1 }
         major: { priority: 2 }
         minor: { priority: 3 }
         trivial: { priority: 4 }
       label_map: { ui: "<label-uuid>" }
       dedup: { enabled: true }

     github_issues:
       repo: owner/name
       severity_map:
         blocker:  { labels: ["severity:blocker"] }
         critical: { labels: ["severity:critical"] }
         major:    { labels: ["severity:major"] }
         minor:    { labels: ["severity:minor"] }
         trivial:  { labels: ["severity:trivial"] }
       default_labels: ["bug", "from-screenshot"]
       dedup:
         enabled: true
         search_template: 'repo:{repo} is:issue is:open in:title {title}'
   ```

2. `config.py`:
   - Locate the file via the **profile-aware** home (`HERMES_HOME` / `Path.home()`),
     **never** hardcode `~/.hermes`.
   - Parse YAML (stdlib-friendly: `yaml` is acceptable if the repo already
     depends on it; otherwise confirm the dependency policy in `AGENTS.md` before
     adding one — Hermes leans stdlib-only).
   - Validate structure; on malformed/missing config return a structured error
     with `remediation` pointing at the expected path and a minimal example.
   - **If the config file is absent, write nothing automatically** beyond
     surfacing a clear remediation (don't silently create files outside the
     documented path).

3. `mapping.py`:
   - `to_payload(bug_report: dict, target: str, cfg: dict, project: str | None) -> dict`.
   - Resolve `severity` via `severity_map`; if a severity has no mapping →
     structured error (don't guess).
   - Expand `{placeholder}` templates from the `BugReport` (`title`,
     `component_hint`, etc.). Missing placeholder → structured error.
   - Compose the body text (summary + steps + expected/actual) per tracker
     conventions (Jira ADF/markup vs GitHub/Linear markdown — keep it simple and
     documented; do not over-engineer rich formatting in v1).
   - **Pure function, no network**, so it's fully unit-testable.

4. `tests/test_mapping.py`: feed sample Jira and Linear (and GitHub if selected)
   configs + a fixed `BugReport`; assert the produced payloads. Cover the
   failure modes: **missing custom field placeholder**, **unmapped severity**,
   **unknown target**.

**Gate:** `scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/test_mapping.py` → green.
**Then commit** `phase 4: config + mapping layer + tests`.

---

## 6. Phase 5 — REST clients + idempotency

**Goal:** `clients.py` exposes one create-issue client per selected tracker, with
auth, timeouts, bounded retries, and **dedup so re-running on the same bug
doesn't spam tickets**.

**Prompt hint:** prefix with `think hard` — auth + idempotency + error mapping is
the second-hardest area.

**Do:**

1. One client per target with a common shape:
   - `find_duplicate(bug_report, cfg) -> existing_url | None` (uses the
     `dedup` template from config; skip if `dedup.enabled` is false).
   - `create_issue(payload, cfg) -> {"url": ..., "id": ...}`.
   - Auth from `requires_env` vars (`JIRA_EMAIL`+`JIRA_API_TOKEN` basic,
     `LINEAR_API_KEY` header, `GITHUB_TOKEN` bearer). **Read tokens only when the
     client is actually used.**
   - **HTTPS + explicit timeout** on every request; **bounded** retries (e.g. 2,
     on connection errors / 5xx only — never retry a 4xx).
2. **Idempotency:** the handler will call `find_duplicate` *before* `create_issue`.
   If a duplicate exists, return it as a success-with-`deduped: true` instead of
   creating a second ticket.
3. **Error mapping → structured errors:** map `401/403` → "invalid or missing
   credentials" with a `remediation` naming the env var; `404` → "project/repo
   not found"; timeouts → "tracker unreachable". Never leak tokens in the error.
4. `tests/test_clients.py`: **mock the HTTP layer** (e.g. `responses`/`httpretty`
   if already available, else monkeypatch the transport). Cover: create success,
   `401`, timeout, and **duplicate-exists → no create**. CI has no network and no
   tokens (run_tests.sh unsets them), so everything here must be mocked.

**Gate:** `scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/test_clients.py` → green.
**Then commit** `phase 5: rest clients + idempotency + tests`.

---

## 7. Phase 6 — Wire the handler end-to-end (+ approval gate)

**Goal:** `report_bug_from_screenshot` runs the full pipeline:
**validate → vision → map → dedup → create → return**.

**Do:**

1. Implement `handle_report_bug(params, **kwargs)` in `__init__.py`:
   - Validate inputs **before any side-effect**: `image_path` exists and is a
     file (realpath); `target` resolves (param → config `default_target`); the
     selected target's credentials are present (else structured `remediation`
     error).
   - `vision.extract(...)` → `BugReport`.
   - `mapping.to_payload(...)` → tracker payload.
   - `clients.find_duplicate(...)`; if found, return
     `{"success": true, "deduped": true, "ticket_url": <url>}`.
   - Else `clients.create_issue(...)`; return
     `{"success": true, "ticket_url": <url>, "summary": <short>}`.
   - All returns are `json.dumps(...)`. Keep the payload **bounded** (URL + short
     summary, not the whole body).
2. **Approval gate (if Q4 = yes):** register a `pre_approval_request` hook so the
   operator confirms before the `create_issue` POST. Make the approval message
   show the target, project, and title — enough to decide, no secrets.
3. `tests/test_handler.py`: full pipeline with **vision mocked and HTTP mocked**.
   Cover: happy path (creates, returns URL); dedup path (returns existing,
   no create); missing-credentials path (structured error, no network); invalid
   image path (structured error).

**Gate:** run the **whole** plugin suite —
`scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/` → green.
**Then commit** `phase 6: end-to-end handler + approval gate`.

---

## 8. Phase 7 — Harden, document, finalize

**Goal:** pass the full security/tool-design checklist, write the README, and
leave the plugin installable.

**Do:**

1. **Re-read `AGENTS.md`** (it changes; do this before finalizing) and reconcile.
2. Walk the §2.8 checklist line by line and fix gaps:
   - redacted logging verified (grep your own log calls for token vars);
   - no reads of `.env` / `auth.json` beyond declared `requires_env`;
   - HTTPS + timeout on every request confirmed;
   - LLM output treated as untrusted (injection note in code);
   - structured errors everywhere.
3. `README.md` must cover: what it does, the `report_bug_from_screenshot` tool,
   the `~/.hermes/bug-tickets.yaml` schema with a copy-paste example, required
   env vars per tracker, and an explicit **"privilege surface"** section (it
   reads a local image, calls the active LLM, and makes authenticated writes to
   your tracker).
4. **Lint/format** per repo conventions (use the repo's configured tools; do not
   introduce a new formatter).
5. **Optional live smoke (only if Q2 provided credentials):** install locally and
   run one real screenshot through it.

   ```bash
   # local install for a manual smoke test
   ln -s "$(pwd)/dev-plugins/hermes-bug-vision-ticket" ~/.hermes/plugins/hermes-bug-vision-ticket
   HERMES_LOG_LEVEL=DEBUG hermes -z "report this bug screenshot: /tmp/bug.png"
   tail -f ~/.hermes/logs/agent.log
   ```

   If Q2 = mocked-only, **skip this step** — the suite already proves correctness.
6. **Publish (only if Q3 provided a remote):** extract the plugin dir to its own
   repo and push; otherwise leave the local git history in place.

   ```bash
   # others would then install via:
   hermes plugins install <you>/hermes-bug-vision-ticket --enable
   ```

**Gate:** full suite green one final time +
`README.md`, `DECISIONS.md` present. **Then commit** `phase 7: hardening + docs`.

---

## 9. Definition of done

- [ ] `scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/` exits `0`.
- [ ] Plugin discovered → `report_bug_from_screenshot` registered under toolset
      `bug_vision_ticket` (proven by `test_register.py`).
- [ ] Pipeline works end-to-end against mocked vision + mocked HTTP.
- [ ] Dedup prevents duplicate tickets; approval gate present if Q4 = yes.
- [ ] `~/.hermes/bug-tickets.yaml` schema documented with an example.
- [ ] Security checklist (§2.8) satisfied; README documents the privilege surface.
- [ ] `DECISIONS.md` records: Hermes commit/tag built against, trackers selected,
      `requires_env` strategy, and any default applied for an unanswered question.
- [ ] One git commit per phase.

---

## 10. Common pitfalls (pre-empt these)

| Pitfall | Correct move |
|---|---|
| Running bare `pytest` | Always `scripts/run_tests.sh` (CI parity). |
| Inventing `requires_hermes_version` / `kind:` in `plugin.yaml` | Only `PluginManifest` fields exist; pin Hermes at runtime if needed. |
| Passing `description=` to `register_tool` | `description` lives **inside** the tool schema dict. |
| Wiring your own LLM/HTTP client for the model call | Use `ctx.llm.complete_structured`; the host handles provider/auth and vision routing. |
| "Managing" vision / picking the vision model | The host auto-routes to `auxiliary.vision`; the plugin decides nothing. |
| Over-investing in the vision call, under-investing in mapping | Vision is ~1 call; **mapping is ~40% of the effort.** Budget accordingly. |
| Tests that read ambient credentials | `run_tests.sh` unsets them → mock LLM and HTTP everywhere. |
| Hardcoding `~/.hermes` | Use the profile-aware home (`HERMES_HOME` / `Path.home()`). |
| Logging tokens or echoing `.env` | Redact; read only declared `requires_env`. |
| Creating duplicate tickets on re-run | `find_duplicate` before `create_issue`. |
| Returning a dict from the handler | Handlers return `json.dumps(...)`. |
| Unbounded tool output | Return URL + short summary, not the whole ticket body. |

---

## 11. Reference — useful commands

```bash
# Setup
git clone https://github.com/NousResearch/hermes-agent.git && cd hermes-agent
./setup-hermes.sh

# Tests (CI parity — the only sanctioned runner)
scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/
scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/test_mapping.py::test_jira_payload

# Debug a live run
HERMES_LOG_LEVEL=DEBUG hermes -z "report this bug screenshot: /tmp/bug.png"
tail -f ~/.hermes/logs/agent.log

# Plugin lifecycle
hermes plugins list
hermes plugins enable hermes-bug-vision-ticket
hermes plugins install <you>/hermes-bug-vision-ticket --enable
```

**Critical source paths to consult while implementing**

| For | Path |
|---|---|
| `ctx.llm` (PluginLlm) — vision call shape | `agent/plugin_llm.py` |
| `PluginManager`, `PluginManifest`, `VALID_HOOKS` | `hermes_cli/plugins.py` |
| Tool registry | `tools/registry.py` |
| Hard rules / test policy | `AGENTS.md` |
| CI parity script | `scripts/run_tests.sh` |
| Official plugin contract guide | `hermes-agent.nousresearch.com/docs` → "Build a Hermes Plugin" |
| Vision-with-image example to mirror | `hermes-example-plugins/plugin-llm-example` |

> **Version caveat:** Hermes moves fast (hundreds of commits between minor
> releases) and a few contract details live only in source. Pin the commit you
> build against, re-read `AGENTS.md` before finalizing, and prefer patterns from
> the latest release over older blog/example snippets.
