"""Phase 2 gate: the plugin is discovered and registers its tool correctly.

Verified against the REAL hermes-agent API (commit pinned in DECISIONS.md):
the tool lands in the global ``tools.registry`` singleton as a ``ToolEntry``
object (``.toolset`` is an attribute, not a dict key), and the PluginManager
tracks the name in ``_plugin_tool_names``.
"""

from __future__ import annotations

import json

TOOL_NAME = "report_bug_from_screenshot"
TOOLSET = "bug_vision_ticket"


def test_plugin_discovered_and_loaded(loaded_manager):
    # The plugin loaded and is enabled (key == dir name == manifest name).
    assert "hermes-bug-vision-ticket" in loaded_manager._plugins
    loaded = loaded_manager._plugins["hermes-bug-vision-ticket"]
    assert loaded.enabled, f"plugin failed to load: {getattr(loaded, 'error', None)}"
    assert not loaded.error


def test_tool_registered_with_expected_toolset(loaded_manager):
    # PluginManager tracks the tool name.
    assert TOOL_NAME in loaded_manager._plugin_tool_names

    # The tool is in the global registry under the expected toolset. ToolEntry
    # is an OBJECT with attributes (NOT a subscriptable dict).
    from tools.registry import registry

    entry = registry.get_entry(TOOL_NAME)
    assert entry is not None, f"{TOOL_NAME} not registered"
    assert entry.toolset == TOOLSET
    assert callable(entry.handler)


def test_tool_schema_is_well_formed(loaded_manager):
    from tools.registry import registry

    entry = registry.get_entry(TOOL_NAME)
    schema = entry.schema
    assert schema["name"] == TOOL_NAME
    # Description must live inside the schema (Hermes reads schema["description"]).
    assert schema.get("description"), "tool schema must carry a description"
    params = schema["parameters"]
    assert params["type"] == "object"
    assert "image_path" in params["properties"]
    assert params["required"] == ["image_path"]
    # target enum must match the supported trackers.
    assert params["properties"]["target"]["enum"] == ["jira", "linear", "github_issues"]


def test_handler_returns_structured_json_string(loaded_manager):
    from tools.registry import registry

    entry = registry.get_entry(TOOL_NAME)
    # Handlers MUST return a JSON string (hermes-agent hard rule). A bad image
    # path fails fast (before any LLM/network) with a structured error.
    out = entry.handler({"image_path": "/tmp/does-not-exist.png"})
    assert isinstance(out, str)
    payload = json.loads(out)
    assert payload["success"] is False
    assert payload["error"] == "image_not_found"
    assert "remediation" in payload


def test_pre_tool_call_hook_registered(loaded_manager):
    # The approval gate hook is registered for pre_tool_call.
    hooks = loaded_manager._hooks.get("pre_tool_call", [])
    assert hooks, "pre_tool_call approval hook not registered"
