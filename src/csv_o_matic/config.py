"""Server configuration: where the data and process maps live, and the limits on every response."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

CONFIG_ENV_VAR = "CSV_O_MATIC_CONFIG"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Limits:
    # Response bounds
    max_rows_per_response: int = 25
    max_output_bytes: int = 120_000  # the whole MCP result: structured content plus the same JSON as text
    max_cell_chars: int = 2_000
    max_sample_rows: int = 10
    max_files_per_listing: int = 200
    # Scan bounds
    max_concurrent_scans: int = 2
    max_scan_seconds: float = 120.0
    progress_interval_seconds: float = 2.0
    # Memory bounds
    chunk_bytes: int = 4 * 1024 * 1024
    max_record_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ConfigError(f"limits.{f.name} must be a positive number, got {value!r}")
        if self.max_output_bytes < 16_000:
            raise ConfigError("limits.max_output_bytes must be at least 16000")
        if self.max_record_bytes < self.chunk_bytes:
            raise ConfigError("limits.max_record_bytes must be at least limits.chunk_bytes")


@dataclass(frozen=True)
class Config:
    data_root: Path
    process_maps_dir: Path
    limits: Limits = field(default_factory=Limits)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load configuration from a YAML file (explicit path, else $CSV_O_MATIC_CONFIG)."""
    path = path or os.environ.get(CONFIG_ENV_VAR)
    if not path:
        raise ConfigError(f"No configuration file: pass --config or set {CONFIG_ENV_VAR}")
    config_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read configuration file {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: top level must be a mapping")

    unknown = set(raw) - {"data_root", "process_maps_dir", "limits"}
    if unknown:
        raise ConfigError(f"{config_path}: unknown keys {sorted(unknown)}")
    for key in ("data_root", "process_maps_dir"):
        if not raw.get(key):
            raise ConfigError(f"{config_path}: '{key}' is required")

    def resolve(value: str) -> Path:
        p = Path(str(value)).expanduser()
        return (p if p.is_absolute() else config_path.parent / p).resolve()

    raw_limits = raw.get("limits") or {}
    known = {f.name for f in fields(Limits)}
    if not isinstance(raw_limits, dict) or set(raw_limits) - known:
        raise ConfigError(f"{config_path}: 'limits' may only contain {sorted(known)}")
    return Config(
        data_root=resolve(raw["data_root"]),
        process_maps_dir=resolve(raw["process_maps_dir"]),
        limits=Limits(**raw_limits),
    )
