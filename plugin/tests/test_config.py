"""Phase 4 gate (config): load + validate ~/.hermes/bug-tickets.yaml.

Covers env-var expansion, target/default resolution, require_approval, and the
failure modes (missing file, bad YAML, no targets, unknown/invalid target).
HERMES_HOME is redirected to a tmp dir per test (env -i already blanks ambient).
"""

from __future__ import annotations

import pytest

from hermes_plugins.hermes_bug_vision_ticket import config
from hermes_plugins.hermes_bug_vision_ticket.errors import BugTicketError

GOOD = """\
default_target: jira
require_approval: true
targets:
  jira:
    base_url: ${JIRA_BASE_URL}
    project_key: ENG
  github_issues:
    repo: acme/web
"""


def _write(home, text):
    (home / "bug-tickets.yaml").write_text(text, encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "hermes_home"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def test_load_and_env_expansion(home, monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://acme.atlassian.net")
    _write(home, GOOD)
    cfg = config.load_config()
    assert cfg["targets"]["jira"]["base_url"] == "https://acme.atlassian.net"
    assert config.require_approval(cfg) is True


def test_env_expansion_unset_becomes_empty(home):
    _write(home, GOOD)
    cfg = config.load_config()
    assert cfg["targets"]["jira"]["base_url"] == ""  # JIRA_BASE_URL unset


def test_resolve_target_name(home):
    _write(home, GOOD)
    cfg = config.load_config()
    assert config.resolve_target_name(cfg, None) == "jira"  # default_target
    assert config.resolve_target_name(cfg, "github_issues") == "github_issues"


def test_resolve_target_not_configured(home):
    _write(home, GOOD)
    cfg = config.load_config()
    with pytest.raises(BugTicketError) as ei:
        config.resolve_target_name(cfg, "linear")
    assert ei.value.error == "target_not_configured"


def test_single_target_no_default(home):
    _write(home, "targets:\n  github_issues:\n    repo: acme/web\n")
    cfg = config.load_config()
    assert config.resolve_target_name(cfg, None) == "github_issues"


def test_multiple_targets_no_default_errors(home):
    _write(home, "targets:\n  jira:\n    project_key: ENG\n  github_issues:\n    repo: a/b\n")
    cfg = config.load_config()
    with pytest.raises(BugTicketError) as ei:
        config.resolve_target_name(cfg, None)
    assert ei.value.error == "no_target_selected"


def test_require_approval_defaults_true(home):
    _write(home, "targets:\n  github_issues:\n    repo: a/b\n")
    cfg = config.load_config()
    assert config.require_approval(cfg) is True


def test_require_approval_false(home):
    _write(home, "require_approval: false\ntargets:\n  github_issues:\n    repo: a/b\n")
    cfg = config.load_config()
    assert config.require_approval(cfg) is False


def test_missing_config(home):
    with pytest.raises(BugTicketError) as ei:
        config.load_config()
    assert ei.value.error == "config_missing"
    assert "bug-tickets.yaml" in ei.value.remediation


def test_invalid_yaml(home):
    _write(home, "targets: : : not yaml\n  - broken")
    with pytest.raises(BugTicketError) as ei:
        config.load_config()
    assert ei.value.error in {"config_invalid_yaml", "config_invalid"}


def test_no_targets(home):
    _write(home, "default_target: jira\n")
    with pytest.raises(BugTicketError) as ei:
        config.load_config()
    assert ei.value.error == "config_no_targets"


def test_unknown_target_in_config(home):
    _write(home, "targets:\n  trello:\n    board: x\n")
    with pytest.raises(BugTicketError) as ei:
        config.load_config()
    assert ei.value.error == "config_unknown_target"
