"""Shared fixtures for hermes-bug-vision-ticket tests.

These tests run inside the hermes-agent venv (so ``hermes_cli``, ``tools`` and
``agent`` import), but they live OUTSIDE hermes-agent's own ``tests/`` tree, so
hermes-agent's autouse ``tests/conftest.py`` (HERMES_HOME isolation, env
blanking, plugin-manager reset) does NOT apply here. We therefore isolate
HERMES_HOME ourselves and clean up the process-global tool registry by hand.

The CI-parity runner (``scripts/run_tests.sh``) already runs each test file in a
hermetic ``env -i`` subprocess, so no ambient credentials leak in.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml

# The plugin package directory (parent of this tests/ dir).
PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGIN_NAME = "hermes-bug-vision-ticket"
TOOL_NAME = "report_bug_from_screenshot"
TOOLSET = "bug_vision_ticket"
# How the host imports the package: slug = name with '/'->'__', '-'->'_'.
NS_PARENT = "hermes_plugins"
NS_MODULE = "hermes_plugins.hermes_bug_vision_ticket"


def _purge_plugin_modules() -> None:
    """Drop the plugin's namespace modules so a later load re-execs cleanly."""
    for name in list(sys.modules):
        if name == NS_MODULE or name.startswith(NS_MODULE + "."):
            sys.modules.pop(name, None)


def _bootstrap_plugin_package() -> None:
    """Import the plugin package as ``hermes_plugins.hermes_bug_vision_ticket``.

    Mirrors hermes_cli.plugins._load_directory_module so unit tests can do
    ``from hermes_plugins.hermes_bug_vision_ticket import vision`` (with working
    intra-package relative imports) WITHOUT going through full plugin discovery.
    Runs at conftest import — before pytest collects the test modules in this
    directory — so the package is present when they import it.
    """
    if NS_MODULE in sys.modules:
        return
    if NS_PARENT not in sys.modules:
        ns = types.ModuleType(NS_PARENT)
        ns.__path__ = []  # namespace package
        ns.__package__ = NS_PARENT
        sys.modules[NS_PARENT] = ns
    init = PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        NS_MODULE, init, submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = NS_MODULE
    module.__path__ = [str(PLUGIN_DIR)]
    sys.modules[NS_MODULE] = module
    spec.loader.exec_module(module)


_bootstrap_plugin_package()


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A fresh, isolated HERMES_HOME with our plugin symlinked + enabled.

    Yields the home Path. ``get_hermes_home()`` reads the HERMES_HOME env var
    live, so monkeypatching it is sufficient to redirect plugin discovery.
    """
    home = tmp_path / "hermes_home"
    (home / "plugins").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Load the REAL plugin source by symlinking it into HERMES_HOME/plugins/.
    (home / "plugins" / PLUGIN_NAME).symlink_to(PLUGIN_DIR, target_is_directory=True)

    # Plugins are opt-in: enable by key under plugins.enabled in config.yaml.
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_NAME]}}),
        encoding="utf-8",
    )

    yield home


@pytest.fixture
def loaded_manager(hermes_home):
    """Discover+load plugins into a fresh PluginManager; clean the global registry after."""
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    _purge_plugin_modules()
    mgr = PluginManager()
    mgr.discover_and_load()
    try:
        yield mgr
    finally:
        # tools.registry is a process-global singleton — remove our tool so a
        # subsequent test in this file gets a clean slate.
        try:
            registry.deregister(TOOL_NAME)
        except Exception:
            pass
        _purge_plugin_modules()
