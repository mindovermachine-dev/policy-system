"""Tests for ps_service.logging.models.LogEntry.to_json_line (AC#3)."""

from __future__ import annotations

import json

from ps_service.logging.models import LogEntry


def test_json_line_when_all_fields_set_then_includes_every_convention_field() -> None:
    entry = LogEntry(
        component="ingestion",
        action="fetch_regulation",
        run_id="run-123",
        entity_id="reg-1",
        outcome="success",
        duration_ms=12.5,
        timestamp=1_700_000_000.0,
        extra=(("note", "hello"),),
    )

    line = entry.to_json_line()

    assert line.endswith("\n")
    payload = json.loads(line)
    assert payload["component"] == "ingestion"
    assert payload["action"] == "fetch_regulation"
    assert payload["run_id"] == "run-123"
    assert payload["entity_id"] == "reg-1"
    assert payload["outcome"] == "success"
    assert payload["duration_ms"] == 12.5
    assert payload["timestamp"] == 1_700_000_000.0
    assert payload["note"] == "hello"


def test_json_line_when_entity_id_is_string_then_emitted_as_is() -> None:
    entry = LogEntry(component="ingestion", action="fetch_regulation", entity_id="reg-1")

    payload = json.loads(entry.to_json_line())

    assert payload["entity_id"] == "reg-1"


def test_json_line_when_entity_id_is_tuple_then_emitted_as_json_array() -> None:
    entry = LogEntry(component="ingestion", action="fetch_regulation", entity_id=("reg-1", "reg-2"))

    payload = json.loads(entry.to_json_line())

    assert payload["entity_id"] == ["reg-1", "reg-2"]


def test_json_line_when_optional_fields_none_then_omitted() -> None:
    entry = LogEntry(component="ingestion", action="fetch_regulation")

    payload = json.loads(entry.to_json_line())

    assert "run_id" not in payload
    assert "entity_id" not in payload
    assert "outcome" not in payload
    assert "duration_ms" not in payload
    assert payload["component"] == "ingestion"
    assert payload["action"] == "fetch_regulation"
    assert "timestamp" in payload


def test_json_line_when_extra_key_collides_with_convention_field_then_convention_wins() -> None:
    entry = LogEntry(
        component="ingestion",
        action="fetch_regulation",
        outcome="success",
        extra=(("outcome", "clobbered"), ("component", "also-clobbered")),
    )

    payload = json.loads(entry.to_json_line())

    assert payload["outcome"] == "success"
    assert payload["component"] == "ingestion"
