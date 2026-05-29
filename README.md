# hermes-bug-vision-ticket

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that turns
a bug **screenshot** into a structured **ticket** in Jira / Linear / GitHub
Issues, via a single tool: `report_bug_from_screenshot`.

## Repo layout

| Path | What it is |
|------|------------|
| [`plugin/`](plugin/) | **The plugin** (`plugin.yaml`, `register(ctx)`, vision/mapping/clients/config, tests). See [`plugin/README.md`](plugin/README.md). |
| `hermes-agent/` | A local clone of the Hermes host — **gitignored** build/test tooling (provides `hermes_cli` for imports and `scripts/run_tests.sh` for CI-parity testing). Not part of this plugin's source. |
| [`DECISIONS.md`](DECISIONS.md) | Pinned Hermes commit, the source-verified plugin contract, and every autonomous choice made building this. |
| [`hermes-bug-vision-ticket-plan.md`](hermes-bug-vision-ticket-plan.md) | The original implementation plan. |

## Quickstart

See [`plugin/README.md`](plugin/README.md) for the full tool reference,
`~/.hermes/bug-tickets.yaml` config schema, per-tracker env vars, the approval
gate, install steps, and the **privilege surface**.

```bash
# Install into your Hermes home and enable it:
ln -s "$(pwd)/plugin" ~/.hermes/plugins/hermes-bug-vision-ticket
# then add `hermes-bug-vision-ticket` under plugins.enabled in ~/.hermes/config.yaml
```

## Running the tests

Tests run against the real Hermes plugin contract with the LLM and all HTTP
mocked — no provider, no network, no credentials needed.

```bash
# one-time: set up the host clone + venv the runner expects
git clone --depth 1 https://github.com/NousResearch/hermes-agent.git
( cd hermes-agent && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" )
mkdir -p hermes-agent/dev-plugins
ln -sfn ../../plugin hermes-agent/dev-plugins/hermes-bug-vision-ticket

# run (CI-parity, hermetic):
( cd hermes-agent && bash scripts/run_tests.sh dev-plugins/hermes-bug-vision-ticket/tests/ )
```
