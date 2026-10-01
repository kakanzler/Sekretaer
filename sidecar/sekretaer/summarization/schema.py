"""Strict validation against ``contracts/notes/note-1.0.schema.json`` (bundled copy, kept identical
by ``tests/test_contracts.py``)."""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


@cache
def load_schema(name: str) -> dict[str, Any]:
    text = resources.files("sekretaer.schemas").joinpath(name).read_text(encoding="utf-8")
    return json.loads(text)


@cache
def note_validator() -> Draft202012Validator:
    schema = load_schema("note-1.0.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@cache
def event_validator() -> Draft202012Validator:
    schema = load_schema("event-1.0.schema.json")
    return Draft202012Validator(schema, format_checker=FormatChecker())


def note_errors(note: Any, limit: int = 10) -> list[str]:
    """Human-readable error list with JSON paths only (never the offending values)."""
    errors = []
    for err in sorted(note_validator().iter_errors(note), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{path}: {err.validator} constraint failed")
        if len(errors) >= limit:
            break
    return errors


def collect_evidence_ids(note: dict[str, Any]) -> set[str]:
    ids: set[str] = set()

    def walk(items: list[dict[str, Any]]) -> None:
        for it in items:
            ids.update(it.get("evidenceSegmentIds") or [])
            if "children" in it:
                walk(it["children"])

    cornell = note.get("cornell", {})
    walk(cornell.get("cues", []))
    walk(cornell.get("notes", []))
    walk(note.get("bullets", []))
    walk(note.get("decisions", []))
    walk(note.get("actions", []))
    walk(note.get("openQuestions", []))
    return ids
