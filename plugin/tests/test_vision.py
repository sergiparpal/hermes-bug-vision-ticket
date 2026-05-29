"""Phase 3 gate: vision core turns a screenshot into a validated BugReport.

ctx.llm is fully mocked — no real LLM call (CI has no provider and no network).
We assert the happy path, severity coercion, schema rejection, unparseable
output, and path-validation failures, plus that the structured call is shaped
correctly (image bytes + mime + instructions + json_schema).
"""

from __future__ import annotations

import pytest

from hermes_plugins.hermes_bug_vision_ticket import vision
from hermes_plugins.hermes_bug_vision_ticket.errors import BugTicketError
from hermes_plugins.hermes_bug_vision_ticket.schemas import BUG_REPORT_INPUT_SCHEMA

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-pixels" * 8


class _FakeResult:
    def __init__(self, parsed=None, text="", content_type="json"):
        self.parsed = parsed
        self.text = text
        self.content_type = content_type
        self.provider = "fake"
        self.model = "fake-vlm"


class _FakeLLM:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


class _FakeCtx:
    def __init__(self, result):
        self.llm = _FakeLLM(result)


def _png(tmp_path, name="bug.png", data=PNG_BYTES):
    p = tmp_path / name
    p.write_bytes(data)
    return p


_FULL_REPORT = {
    "title": "Save button overlaps footer",
    "summary": "The Save button renders on top of the footer text.",
    "steps_to_reproduce": ["Open settings", "Scroll to bottom"],
    "expected_behavior": "Save button sits above the footer.",
    "actual_behavior": "Save button overlaps the footer, hiding the copyright.",
    "severity": "major",
    "component_hint": "settings-page",
    "ui_elements_observed": ["Save button", "footer"],
    "visible_text": ["Save", "© 2026 Example"],
    "confidence": "high",
}


def test_happy_path_returns_normalized_report(tmp_path):
    ctx = _FakeCtx(_FakeResult(parsed=dict(_FULL_REPORT)))
    img = _png(tmp_path)

    report = vision.extract_bug_report(ctx, str(img))

    assert report["title"] == _FULL_REPORT["title"]
    assert report["severity"] == "major"
    assert report["confidence"] == "high"
    assert report["steps_to_reproduce"] == ["Open settings", "Scroll to bottom"]


def test_structured_call_is_shaped_correctly(tmp_path):
    ctx = _FakeCtx(_FakeResult(parsed=dict(_FULL_REPORT)))
    img = _png(tmp_path)

    vision.extract_bug_report(ctx, str(img))

    (call,) = ctx.llm.calls
    assert call["instructions"] == vision.SYSTEM_INSTRUCTIONS
    # We hand the host the RELAXED schema (so normalization can fix off-enum
    # output the host would otherwise reject); the strict schema is validated
    # locally afterwards.
    assert call["json_schema"] is BUG_REPORT_INPUT_SCHEMA
    assert "required" not in BUG_REPORT_INPUT_SCHEMA  # host must not pre-reject
    assert BUG_REPORT_INPUT_SCHEMA["additionalProperties"] is True
    assert "enum" not in BUG_REPORT_INPUT_SCHEMA["properties"]["severity"]
    # Other half of the call contract.
    assert call["schema_name"] == "bug_report"
    assert call["max_tokens"] == 1500
    assert call["purpose"] == "bug-vision-extract"
    blocks = call["input"]
    assert len(blocks) == 1
    img_block = blocks[0]
    assert img_block["type"] == "image"
    assert img_block["data"] == PNG_BYTES  # raw bytes, read from disk
    assert img_block["mime_type"] == "image/png"
    assert img_block["file_name"] == "bug.png"


def test_jpeg_mime_inferred(tmp_path):
    ctx = _FakeCtx(_FakeResult(parsed=dict(_FULL_REPORT)))
    img = _png(tmp_path, name="shot.jpeg")
    vision.extract_bug_report(ctx, str(img))
    assert ctx.llm.calls[0]["input"][0]["mime_type"] == "image/jpeg"


def test_sparse_but_valid_report_gets_typed_defaults(tmp_path):
    sparse = {
        "title": "Crash on load",
        "summary": "App shows a white screen.",
        "severity": "critical",
        "actual_behavior": "White screen with no content.",
    }
    ctx = _FakeCtx(_FakeResult(parsed=sparse))
    report = vision.extract_bug_report(ctx, str(_png(tmp_path)))
    # Missing list fields default to [] (correct type), confidence defaults.
    assert report["steps_to_reproduce"] == []
    assert report["ui_elements_observed"] == []
    assert report["visible_text"] == []
    assert report["confidence"] == "medium"
    assert report["component_hint"] is None


@pytest.mark.parametrize(
    "raw,expected",
    [("high", "critical"), ("low", "minor"), ("URGENT", "critical"),
     ("cosmetic", "trivial"), ("nonsense", "major"), ("", "major")],
)
def test_severity_coercion(tmp_path, raw, expected):
    rep = dict(_FULL_REPORT)
    rep["severity"] = raw
    ctx = _FakeCtx(_FakeResult(parsed=rep))
    report = vision.extract_bug_report(ctx, str(_png(tmp_path)))
    assert report["severity"] == expected


def test_schema_invalid_output_rejected(tmp_path):
    # Missing required 'title' -> rejected after normalization.
    bad = dict(_FULL_REPORT)
    del bad["title"]
    ctx = _FakeCtx(_FakeResult(parsed=bad))
    with pytest.raises(BugTicketError) as ei:
        vision.extract_bug_report(ctx, str(_png(tmp_path)))
    assert ei.value.error == "vision_invalid"


def test_unparseable_output_rejected(tmp_path):
    ctx = _FakeCtx(_FakeResult(parsed=None, text="not json at all", content_type="text"))
    with pytest.raises(BugTicketError) as ei:
        vision.extract_bug_report(ctx, str(_png(tmp_path)))
    assert ei.value.error == "vision_unparseable"


def test_host_validation_valueerror_becomes_structured_error(tmp_path):
    # The host raises ValueError if its own schema validation/parse fails; we
    # convert that to a clean vision_unparseable instead of a generic crash.
    class _RaisingLLM:
        def complete_structured(self, **kwargs):
            raise ValueError("Plugin LLM structured output did not match schema: ...")

    class _Ctx:
        llm = _RaisingLLM()

    with pytest.raises(BugTicketError) as ei:
        vision.extract_bug_report(_Ctx(), str(_png(tmp_path)))
    assert ei.value.error == "vision_unparseable"


def test_text_fallback_parse(tmp_path):
    # Host didn't pre-parse, but .text holds valid JSON -> we parse it.
    import json

    ctx = _FakeCtx(_FakeResult(parsed=None, text=json.dumps(_FULL_REPORT)))
    report = vision.extract_bug_report(ctx, str(_png(tmp_path)))
    assert report["title"] == _FULL_REPORT["title"]


def test_missing_file_rejected(tmp_path):
    ctx = _FakeCtx(_FakeResult(parsed=dict(_FULL_REPORT)))
    with pytest.raises(BugTicketError) as ei:
        vision.extract_bug_report(ctx, str(tmp_path / "nope.png"))
    assert ei.value.error == "image_not_found"
    assert not ctx.llm.calls  # never reached the model


def test_unsupported_extension_rejected(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_bytes(b"hello")
    ctx = _FakeCtx(_FakeResult(parsed=dict(_FULL_REPORT)))
    with pytest.raises(BugTicketError) as ei:
        vision.extract_bug_report(ctx, str(p))
    assert ei.value.error == "unsupported_image_type"


def test_empty_image_rejected(tmp_path):
    p = tmp_path / "empty.png"
    p.write_bytes(b"")
    ctx = _FakeCtx(_FakeResult(parsed=dict(_FULL_REPORT)))
    with pytest.raises(BugTicketError) as ei:
        vision.extract_bug_report(ctx, str(p))
    assert ei.value.error == "empty_image"
