"""The sidecar stays in sync with contracts/: bundled schemas, generated OpenAPI, endpoint table."""

from __future__ import annotations

import importlib.util
import re

from sekretaer.summarization.schema import note_validator

from .conftest import REPO


def test_bundled_schemas_are_identical_to_contracts() -> None:
    pkg = REPO / "sidecar" / "sekretaer" / "schemas"
    for rel in ("notes/note-1.0.schema.json", "events/event-1.0.schema.json"):
        name = rel.split("/")[1]
        assert (pkg / name).read_bytes() == (REPO / "contracts" / rel).read_bytes(), rel


def test_note_schema_is_valid_draft_2020_12() -> None:
    assert note_validator() is not None


def _load_export_script():
    spec = importlib.util.spec_from_file_location("export_openapi", REPO / "scripts" / "export-openapi.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_openapi_file_is_up_to_date() -> None:
    generated = _load_export_script().generate()
    committed = (REPO / "contracts" / "openapi" / "openapi.json").read_text(encoding="utf-8")
    assert committed == generated, "run: cd sidecar && uv run python ../scripts/export-openapi.py"


def test_every_contract_endpoint_exists_in_openapi() -> None:
    import json

    spec = json.loads((REPO / "contracts" / "openapi" / "openapi.json").read_text(encoding="utf-8"))
    ops = {(m.upper(), re.sub(r"\{[^}]+\}", "{}", p)) for p, v in spec["paths"].items() for m in v}
    text = (REPO / "contracts" / "api-v1.md").read_text(encoding="utf-8")
    found = re.findall(r"`(GET|POST|PUT|PATCH|DELETE) (/[^` ]+)`", text)
    found += [("PUT", "/settings")]  # "GET /settings / PUT /settings" row
    assert len(found) >= 25
    for method, path in found:
        path = path.split("?")[0].removeprefix("/api/v1")
        norm = "/api/v1" + re.sub(r"\{[^}]+\}", "{}", path)
        assert (method, norm) in ops, (method, path)
