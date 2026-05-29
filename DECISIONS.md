# DECISIONS.md

Autonomous and operator-confirmed choices made while implementing
`hermes-bug-vision-ticket` per `hermes-bug-vision-ticket-plan.md`. The operator
can review these *after* the run instead of blocking *during* it.

## Build target (pinned)

- **Hermes commit built against:** `75cd420b3ba1b83185020c6d4506d7cc53b12e2b`
  (committed 2026-05-29, default branch). Hermes APIs move fast; this is the
  exact commit the plugin contract was verified against and tested on.
- Cloned via `git clone --depth 1 https://github.com/NousResearch/hermes-agent.git`
  into `./hermes-agent` (gitignored — see Repo layout below).

## Phase 1 operator interview (answered 2026-05-29)

| # | Question | Answer |
|---|----------|--------|
| 1 | Trackers for v1 | **All three** (jira, linear, github_issues) |
| 2 | Live smoke test vs mocked | **Mocked only** — live smoke (Phase 7) skipped; suite proves correctness |
| 3 | Approval gate before ticket POST | **Yes** — wire the approval hook |
| 4 | Author / publish | `author: "sergiparpal"`, **local git only**, no remote push |
| 5 | Existing hermes-agent clone vs clone fresh | *(not asked — no clone present)* → **defaulted to clone fresh** into `./hermes-agent` |

## Repo layout

- This repo (`/home/sergi/hermes-bug-vision-ticket`) **is** the plugin's own
  repo (it already held the plan, README, LICENSE). Rather than develop a copy
  buried inside the clone (the plan's literal suggestion, written for the case
  where no plugin repo exists yet), the plugin source lives at this repo root so
  git history / one-commit-per-phase stays clean.
- `./hermes-agent/` is the cloned host runtime — gitignored. It exists only to
  (a) make `hermes_cli` importable and (b) provide `scripts/run_tests.sh`
  (CI-parity runner).
- The plugin dir is symlinked into `hermes-agent/dev-plugins/hermes-bug-vision-ticket`
  so `scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/` resolves
  and `hermes_cli` is importable from inside the host venv. Phase 7 "extract to
  its own repo" is therefore a no-op — it already is its own repo.

## Source-verified contract corrections (the plan diverged from reality)

Verified by reading `hermes-agent` source at commit `75cd420b…` (and empirically
running existing plugin tests). The plan flagged many items as unverified; these
are the actual facts the implementation follows:

| Plan said | Reality (source) | Where |
|-----------|------------------|-------|
| `PluginManager.discover_and_load_from(path)` | No such method. `discover_and_load(force=False)` scans **all** sources (`<repo>/plugins`, `<HERMES_HOME>/plugins`, `./.hermes/plugins`, pip entry-points) using `HERMES_HOME` env. | `hermes_cli/plugins.py:1027` |
| `pm.get_registered_tools()['name']['toolset']` | No such method. Tools live in the global `tools.registry` singleton; `registry._tools[name]` is a `ToolEntry` **object** → read `.toolset` (attribute, not dict). Public: `registry.get_entry(name)`, `get_tool_to_toolset_map()`. `PluginManager._plugin_tool_names` is a `Set[str]`. | `tools/registry.py` |
| `requires_env` missing → plugin **auto-disables** | False. `requires_env` is **informational only** (install-prompt + `hermes config` UI); it never gates loading. Tool availability is gated by a per-tool `check_fn`; missing creds are surfaced at call time. | `hermes_cli/plugins.py:1108-1186` |
| Approval gate = `pre_approval_request` hook | That hook exists but is **observer-only** (return value ignored; fires only for dangerous shell commands). To actually block a tool, use **`pre_tool_call`** returning `{"action":"block","message":...}`. | `hermes_cli/plugins.py:127-167,1666-1707` |
| `complete_structured(..., schema=...)` returns a dict | Param is **`json_schema`** (not `schema`); returns a **`PluginLlmStructuredResult`** — parsed dict is on `.parsed`, `.content_type=="json"` when parsed. All args keyword-only; image block = `{"type":"image","data":<bytes>,"mime_type":...}`, text block key is `text`; the ask goes in `instructions`. | `agent/plugin_llm.py:683-766` |
| Host auto-routes vision to `auxiliary.vision` | False for this path: `PluginLlm` calls `call_llm(task=None)`, so the **user's main model must be multimodal**. No dedicated vision fallback. README documents this. | `agent/plugin_llm.py:949-958`, `agent/auxiliary_client.py:4855` |
| `dev-plugins/` directory convention | Does not exist. Plugins load from `<HERMES_HOME>/plugins/<name>/` (+ repo `plugins/`, project `./.hermes/plugins`, entry-points), and are **opt-in** via `plugins.enabled` in `<HERMES_HOME>/config.yaml`. | `hermes_cli/plugins.py` docstring |
| `run_tests.sh` uses 4 xdist workers | Per-file subprocess isolation via `run_tests_parallel.py`, hermetic `env -i`, 30s per-test timeout. Requires a pre-existing venv at `hermes-agent/.venv`. | `scripts/run_tests.sh` |
| Manifest is a closed set (unknown key errors) | `@dataclass`, parsed field-by-field via `data.get(...)`; unknown YAML keys are silently ignored. No `requires_hermes_version` field. | `hermes_cli/plugins.py:232-266,1352` |

Plugin packages import as `hermes_plugins.<slug>` (`slug` = name with `/`→`__`,
`-`→`_`) with `__path__` set to the plugin dir, so **relative imports
(`from .schemas import ...`) work despite the hyphenated dir name**
(`hermes_cli/plugins.py:1474-1510`). Multi-module layout is safe.

Dependency facts: `pyyaml==6.0.3`, `requests==2.33.0`, `httpx`, `jsonschema` are
all already core/venv deps → no new dependency is added. Only enforced lint is
ruff `PLW1514` (text file ops need `encoding=`) + a windows-footguns check → all
text I/O uses `encoding="utf-8"`, images read as binary.

## requires_env strategy

- **Declare all tracker env vars in `requires_env` (informational), gate nothing
  at load.** Since `requires_env` does not auto-disable, the plugin always loads
  and the tool is always offered. Per-tracker credentials are validated **at call
  time** inside the handler, returning a structured `{success:false, error,
  remediation}` naming the missing env var. No `check_fn` is used (tool stays
  visible so it can explain what's missing). This matches the plan's "load even
  when only some trackers configured" goal.

## Approval gate design (Q4 = yes; adapted to the real hook surface)

- The real blocking hook is **`pre_tool_call`** (not `pre_approval_request`).
- Two-layer, defense-in-depth: (1) the tool has a `confirm: bool` param — the
  handler **never POSTs** unless `confirm=true` (default = a safe **preview**:
  vision+map+dedup, no creation); (2) a `pre_tool_call` hook enforces it — when
  the config's `require_approval` is true (default), an unconfirmed
  `report_bug_from_screenshot` call is **blocked** with a message naming the
  target/project and instructing re-invocation with `confirm=true`. Idempotent
  dedup hits return the existing ticket without needing confirmation.

## Repo layout (final)

- Plugin source in **`./plugin/`** (a subdirectory — keeps the gitignored
  `hermes-agent/` clone out of the plugin's package tree, recursion-safe).
- Gate runs via the host venv: `bash hermes-agent/scripts/run_tests.sh <abs path
  to plugin/tests/...>`. A convenience symlink
  `hermes-agent/dev-plugins/hermes-bug-vision-ticket → ../../plugin` also makes
  the plan's literal `scripts/run_tests.sh dev-plugins/...` command work.
- Tests load the real plugin by symlinking `./plugin` into a per-test temp
  `HERMES_HOME/plugins/hermes-bug-vision-ticket`, enabling it in `config.yaml`,
  and calling `PluginManager().discover_and_load()`.

## Other autonomous choices

- Cloned `hermes-agent` shallow (`--depth 1`); built `.venv` with `pip install -e
  ".[dev]"` (uv not installed; full `setup-hermes.sh` not needed and can't run
  here — no uv/network for it).
