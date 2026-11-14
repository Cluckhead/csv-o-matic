"""Process maps: maintained YAML/JSON files describing each ETL process.

Maps are re-read from disk on every request, so edits take effect immediately and
nothing about them is cached or stored elsewhere.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SUPPORTED_ENCODINGS = {"utf-8", "utf-8-sig", "ascii", "latin-1", "cp1252"}


class _Model(BaseModel):
    # Unknown keys are rejected so a typo in a maintained file is reported, not ignored.
    model_config = ConfigDict(extra="forbid")


class FilenameToken(_Model):
    """One named group of the filename regex, e.g. a business date or a run timestamp."""

    meaning: str
    format: str | None = Field(None, description="strptime format; set for date/time tokens")
    timezone: str | None = None


class FilenameConvention(_Model):
    pattern: str = Field(description="Human-readable pattern, e.g. prices_{business_date}_{run_timestamp}.csv")
    regex: str = Field(description="Regex with one named group per token")
    tokens: dict[str, FilenameToken] = Field(default_factory=dict)
    default_date_token: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "FilenameConvention":
        try:
            compiled = re.compile(self.regex)
        except re.error as exc:
            raise ValueError(f"filename regex does not compile: {exc}") from exc
        missing = set(self.tokens) - set(compiled.groupindex)
        if missing:
            raise ValueError(f"tokens {sorted(missing)} are not named groups in the filename regex")
        dated = [name for name, token in self.tokens.items() if token.format]
        if self.default_date_token is None:
            self.default_date_token = dated[0] if dated else None
        elif self.default_date_token not in dated:
            raise ValueError("default_date_token must name a token that has a 'format'")
        return self

    def parse(self, filename: str) -> dict[str, str] | None:
        """Extract token values from a filename; date/time tokens are returned in ISO form."""
        match = re.fullmatch(self.regex, filename)
        if not match:
            return None
        out: dict[str, str] = {}
        for name, token in self.tokens.items():
            raw = match.group(name)
            if raw is None:
                continue
            if token.format:
                try:
                    parsed = datetime.strptime(raw, token.format)
                except ValueError:
                    return None
                has_time = any(code in token.format for code in ("%H", "%I", "%M", "%S", "%f"))
                out[name] = parsed.isoformat() if has_time else parsed.date().isoformat()
            else:
                out[name] = raw
        return out


class IdentifierColumn(_Model):
    column: str
    identifier_type: str | None = Field(None, description="e.g. ISIN, SEDOL, CUSIP, internal id")
    notes: str | None = None


class CsvFormat(_Model):
    delimiter: str = ","
    encoding: str = "utf-8"

    @field_validator("delimiter")
    @classmethod
    def _delimiter(cls, value: str) -> str:
        if len(value) != 1 or value in '"\r\n' or ord(value) > 127:
            raise ValueError("delimiter must be a single ASCII character other than a quote or newline")
        return value

    @field_validator("encoding")
    @classmethod
    def _encoding(cls, value: str) -> str:
        normalised = value.lower().replace("_", "-").replace("iso-8859-1", "latin-1").replace("latin1", "latin-1")
        if normalised not in SUPPORTED_ENCODINGS:
            raise ValueError(f"encoding must be one of {sorted(SUPPORTED_ENCODINGS)}")
        return normalised


class Folder(_Model):
    id: str
    path: str = Field(description="Relative to the configured data_root, or absolute")
    description: str
    filename_convention: FilenameConvention | None = None
    identifier_columns: list[IdentifierColumn] = Field(default_factory=list)
    record_key_columns: list[str] = Field(
        default_factory=list, description="Columns that together identify one record for a security"
    )
    csv_format: CsvFormat = Field(default_factory=CsvFormat)
    notes: str | None = None


class FieldMapping(_Model):
    from_folder: str
    from_column: str
    to_folder: str
    to_column: str
    rule: str | None = Field(None, description="How the value is carried or transformed")


class Step(_Model):
    id: str
    name: str
    description: str
    reads: list[str] = Field(default_factory=list, description="Folder ids this step reads")
    writes: list[str] = Field(default_factory=list, description="Folder ids this step writes")
    upstream: list[str] = Field(default_factory=list, description="Step ids that feed this step")
    downstream: list[str] = Field(default_factory=list, description="Step ids fed by this step (several = a branch)")
    branch_condition: str | None = Field(None, description="When/why records take this branch")
    field_mappings: list[FieldMapping] = Field(default_factory=list)
    transformation_rules: list[str] = Field(default_factory=list)
    filter_rules: list[str] = Field(default_factory=list, description="Known reasons a security is dropped here")
    notes: str | None = None


class ProcessMap(_Model):
    id: str
    name: str
    description: str = Field(description="One-line summary shown when listing processes")
    purpose: str
    schedule: str | None = None
    run_semantics: str = Field(description="How runs, retries, reruns and backfills show up in the files")
    folders: list[Folder]
    steps: list[Step]
    notes: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "ProcessMap":
        folder_ids = [f.id for f in self.folders]
        step_ids = [s.id for s in self.steps]
        for kind, ids in (("folder", folder_ids), ("step", step_ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                raise ValueError(f"duplicate {kind} ids: {dupes}")
        steps = {s.id: s for s in self.steps}
        for step in self.steps:
            for ref in (*step.reads, *step.writes):
                if ref not in folder_ids:
                    raise ValueError(f"step '{step.id}' refers to unknown folder '{ref}'")
            for ref in (*step.upstream, *step.downstream):
                if ref not in steps:
                    raise ValueError(f"step '{step.id}' refers to unknown step '{ref}'")
            for mapping in step.field_mappings:
                for ref in (mapping.from_folder, mapping.to_folder):
                    if ref not in folder_ids:
                        raise ValueError(f"step '{step.id}' field mapping refers to unknown folder '{ref}'")
        # A relationship may be declared from either end; make both ends agree.
        for step in self.steps:
            for ref in step.downstream:
                if step.id not in steps[ref].upstream:
                    steps[ref].upstream.append(step.id)
            for ref in step.upstream:
                if step.id not in steps[ref].downstream:
                    steps[ref].downstream.append(step.id)
        return self


@dataclass(frozen=True)
class FolderRef:
    """A physical folder as the tools address it, merged across every process that uses it."""

    path: str  # as declared (normalised); this is what callers pass as `folder`
    resolved: Path
    usages: list[tuple[ProcessMap, Folder]]

    @property
    def primary(self) -> Folder:
        return self.usages[0][1]

    @property
    def filename_convention(self) -> FilenameConvention | None:
        return next((f.filename_convention for _, f in self.usages if f.filename_convention), None)


@dataclass
class Catalog:
    processes: list[ProcessMap]
    load_errors: list[dict[str, str]]
    data_root: Path

    def find_process(self, name_or_id: str) -> ProcessMap | None:
        wanted = name_or_id.strip().casefold()
        for process in self.processes:
            if process.id.casefold() == wanted or process.name.casefold() == wanted:
                return process
        return None

    def folders(self, process: ProcessMap | None = None) -> list[FolderRef]:
        merged: dict[Path, FolderRef] = {}
        for proc in [process] if process else self.processes:
            for folder in proc.folders:
                declared = Path(folder.path).expanduser()
                resolved = Path(os.path.realpath(declared if declared.is_absolute() else self.data_root / declared))
                ref = merged.get(resolved)
                if ref is None:
                    merged[resolved] = ref = FolderRef(_normalise(folder.path), resolved, [])
                ref.usages.append((proc, folder))
        return list(merged.values())

    def find_folder(self, folder: str) -> FolderRef | None:
        wanted = _normalise(folder)
        refs = self.folders()
        for ref in refs:
            if wanted == ref.path or wanted == str(ref.resolved):
                return ref
        for ref in refs:  # any alias a process declared for the same physical folder
            if any(wanted == _normalise(f.path) for _, f in ref.usages):
                return ref
        return None


def _normalise(path: str) -> str:
    return os.path.normpath(path.strip().replace("\\", "/")) if path.strip() else ""


def load_catalog(maps_dir: Path, data_root: Path) -> Catalog:
    processes: list[ProcessMap] = []
    errors: list[dict[str, str]] = []
    try:
        files = sorted(p for p in maps_dir.iterdir() if p.suffix.lower() in {".yaml", ".yml", ".json"})
    except OSError as exc:
        return Catalog([], [{"file": str(maps_dir), "error": f"cannot read process map directory: {exc}"}], data_root)
    seen: dict[str, str] = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
            raw: Any = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
            process = ProcessMap.model_validate(raw)
        except ValidationError as exc:
            detail = "; ".join(f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in exc.errors())
            errors.append({"file": path.name, "error": detail})
            continue
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append({"file": path.name, "error": str(exc)})
            continue
        if process.id.casefold() in seen:
            errors.append({"file": path.name, "error": f"duplicate process id '{process.id}' (also in {seen[process.id.casefold()]})"})
            continue
        seen[process.id.casefold()] = path.name
        processes.append(process)
    return Catalog(processes, errors, data_root)
