from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from csv_o_matic import schemas
from csv_o_matic.config import Config, Limits
from csv_o_matic.csvscan import ScanControl
from csv_o_matic.service import Service

PROCESS_MAP = """
id: demo
name: Demo Process
description: Two-stage demo.
purpose: Test fixture.
run_semantics: Later run timestamps supersede earlier ones for the same business date.
folders:
  - id: stage_a
    path: stage/a
    description: First stage output.
    filename_convention:
      pattern: "a_{business_date}_{run_timestamp}.csv"
      regex: '^a_(?P<business_date>\\d{8})_(?P<run_timestamp>\\d{8}T\\d{6})\\.csv$'
      tokens:
        business_date: {format: "%Y%m%d", meaning: "As-of date"}
        run_timestamp: {format: "%Y%m%dT%H%M%S", meaning: "Run start, UTC"}
    identifier_columns: [{column: ID, identifier_type: internal}]
  - id: stage_b
    path: stage/b
    description: Second stage output.
  - id: semi
    path: stage/semi
    description: Semicolon-delimited latin-1 files.
    csv_format: {delimiter: ";", encoding: latin-1}
steps:
  - id: first
    name: First
    description: Writes stage a.
    writes: [stage_a]
    downstream: [second, third]
  - id: second
    name: Second
    description: Reads a, writes b.
    reads: [stage_a]
    writes: [stage_b]
    filter_rules: ["Drops rows with Qty = 0."]
  - id: third
    name: Third
    description: Branch reading a.
    branch_condition: Only bonds.
    reads: [stage_a]
    writes: [semi]
"""

TRICKY = (
    "ID,Name,Notes,AltID,Qty\r\n"
    '00123,"Acme, Inc.","line1\nline2",X1,10\r\n'
    "123,Beta Corp,plain,00123,5\r\n"
    '00123,"Say ""hi""",dup,00123,7\r\n'
    " 00123 ,Padded,ws,X9,1\r\n"
    'ABC,"quoted\nmulti\nline, with comma",n,X2,3\r\n'
)


def digest(root: Path) -> dict[str, tuple[str, int]]:
    """Content hash and mtime of every file under root, to prove nothing was modified."""
    return {
        str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*")) if p.is_file()
    }


@pytest.fixture
def env(tmp_path: Path):
    data = tmp_path / "data"
    maps = tmp_path / "maps"
    for folder in ("stage/a", "stage/b", "stage/semi"):
        (data / folder).mkdir(parents=True)
    maps.mkdir()
    (maps / "demo.yaml").write_text(PROCESS_MAP)
    a = data / "stage/a"
    (a / "a_20261112_20261112T180000.csv").write_bytes(TRICKY.encode())
    (a / "a_20261113_20261113T180000.csv").write_text("ID,Name\n1,x\n")
    (a / "a_20261113_20261113T193000.csv").write_text("ID,Name\n1,x\n2,y\n")
    (a / "misc_notes.csv").write_text("ID\n1\n")
    (a / "readme.txt").write_text("not a csv")

    # 100 rows for one security, for truncation and paging.
    with open(a / "many.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ID", "Seq", "Text"])
        for i in range(300):
            w.writerow(["DUP" if i % 3 == 0 else f"S{i}", i, "x" * 50])

    # Wide file: 600 columns.
    with open(a / "wide.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ID"] + [f"px_field_{i:03d}" if i % 50 == 0 else f"col_{i:03d}" for i in range(1, 600)])
        for r in range(20):
            w.writerow([f"W{r:04d}"] + [f"v{r}_{i}" for i in range(1, 600)])

    b = data / "stage/b"
    (b / "left.csv").write_text(
        "ID,Portfolio,Price,Name,Qty\n"
        "00123,P1,100.50,Acme,10\n"
        "00123,P2,100.50,Acme,20\n"
        "00777,P1,5,Solo,1\n"
        "00999,P1,7,OnlyLeft,1\n"
    )
    (b / "right.csv").write_text(
        "sec_id,pf,px,Name,Qty,Extra\n"
        "00123,P2,100.5,Acme,25,e\n"
        "00123,P1,100.50,ACME,10,e\n"
        "00123,P3,1,Acme,1,e\n"
        "00777,P1,5,Solo ,1,e\n"
    )
    (data / "stage/semi" / "semi.csv").write_bytes(
        "id;name;amount\nÄ1;\"Müller; Söhne\";1,5\nB2;Plain;2,5\n".encode("latin-1")
    )
    return data, maps


def make_service(env, **limits) -> Service:
    data, maps = env
    return Service(Config(data_root=data, process_maps_dir=maps, limits=Limits(**limits)))


@pytest.fixture
def service(env) -> Service:
    return make_service(env)


@pytest.fixture
def ctl():
    return lambda seconds=30.0: ScanControl(max_seconds=seconds)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ------------------------------------------------------------------ output schemas
# The published schemas are open (unknown fields are allowed), so that a client never breaks on an
# addition. The tests are stricter: every field any tool returns must be described by its schema.

def strict(schema):
    """A copy of a JSON schema in which objects with declared properties accept nothing else."""
    if isinstance(schema, list):
        return [strict(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {key: strict(value) for key, value in schema.items()}
    if "properties" in out and "additionalProperties" not in out:
        out["additionalProperties"] = False
    return out


_VALIDATORS: dict = {}


def check_schema(tool: str, payload: dict, schema: dict | None = None) -> None:
    """Fail unless `payload` (a complete response, including `tool`) fits the tool's output schema exactly."""
    from jsonschema import Draft202012Validator
    from pydantic import TypeAdapter

    if schema is None:
        if tool not in _VALIDATORS:
            _VALIDATORS[tool] = Draft202012Validator(strict(TypeAdapter(schemas.RESULT_TYPES[tool]).json_schema()))
        validator = _VALIDATORS[tool]
    else:
        validator = Draft202012Validator(strict(schema))
    errors = [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:300]}" for e in validator.iter_errors(payload)]
    assert not errors, f"{tool} response does not fit its output schema: {errors[:5]}"


@pytest.fixture(autouse=True)
def _every_service_result_fits_its_schema(monkeypatch):
    """Check the result of every tool call made by any test against the tool's output schema."""
    for tool in schemas.RESULT_TYPES:
        original = getattr(Service, tool)

        def checked(self, *args, _original=original, _tool=tool, **kwargs):
            result = _original(self, *args, **kwargs)
            check_schema(_tool, json.loads(json.dumps({"tool": _tool, **result})))
            return result

        monkeypatch.setattr(Service, tool, checked)
