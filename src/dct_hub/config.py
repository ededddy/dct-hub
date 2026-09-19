"""Loads charts-tool.yml. Relative paths resolve against the config file's directory."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .auth.config import AccessConfig, AuthConfig, expand_env


class S3Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket: str = ""
    # MinIO / on-prem object storage; omit for AWS.
    endpoint_url: str | None = None
    prefix: str = "artifacts/"
    region: str | None = None
    # Credentials come from the standard env chain (AWS_ACCESS_KEY_ID /
    # AWS_SECRET_ACCESS_KEY), never from this file.


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dir: Path = Path(".hub")
    # Postgres DSN. Set => HA mode: metadata, queue, rate limit and
    # leadership live in Postgres, artifacts in S3, and many replicas share
    # them. Unset => single-node: SQLite + local filesystem under dir.
    postgres: str = ""
    s3: S3Config = Field(default_factory=S3Config)

    @model_validator(mode="after")
    def _check(self) -> "StorageConfig":
        self.postgres = expand_env(self.postgres)
        if self.postgres and not self.s3.bucket:
            raise ValueError("storage.postgres requires storage.s3.bucket (HA artifacts live in S3)")
        if self.s3.bucket and not self.postgres:
            raise ValueError("storage.s3 without storage.postgres is invalid (shared blobs need shared metadata)")
        return self


class RenderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = "html"
    timeout_s: int = 120
    query_cache: Path | None = None


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Artifacts younger than this are served without re-rendering.
    default_ttl_s: int = 3600
    # When true, boards whose date variables all point at past dates are
    # immutable: rendered once, served forever (unless force-rendered).
    frozen_date_vars: bool = True
    # Minimum seconds between renders of the same artifact (force excepted).
    min_interval_s: int = 300
    # Parallel render workers. Forced to 1 when a persistent query cache is
    # configured (DuckDB single-writer limit).
    max_concurrent: int = 2
    # Per-identity cap on render submissions per minute, across all triggers
    # (auto and force — this bounds the force bypass of min_interval_s).
    # Default 30; 0 = unlimited. Deploy-pipeline warming spends the same
    # budget — raise the cap if your warm list is long.
    max_renders_per_minute: int = 30


class RetentionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Off by default: deleting artifacts is a deliberate opt-in.
    enabled: bool = False
    # Prune non-frozen artifacts older than this. Frozen snapshots are never
    # pruned — immutability is their contract.
    max_age_days: int = 30
    # Keep at most this many non-frozen artifacts per board (newest first).
    max_per_board: int = 100
    sweep_interval_s: int = 3600


class WarmBoard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board: str
    # Variable combos to warm; [{}] warms the board's declared defaults.
    vars: list[dict[str, Any]] = Field(default_factory=lambda: [{}])


class WarmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Boards rendered by `POST /api/warm` (the deploy-pipeline warm step).
    boards: list[WarmBoard] = Field(default_factory=list)

    @field_validator("boards", mode="before")
    @classmethod
    def _normalize(cls, value: object) -> object:
        if isinstance(value, list):
            return [{"board": entry} if isinstance(entry, str) else entry for entry in value]
        return value


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_dir: Path
    host: str = "127.0.0.1"
    port: int = 8080
    dct_bin: str = "dct"
    storage: StorageConfig = Field(default_factory=StorageConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    warm: WarmConfig = Field(default_factory=WarmConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    access: AccessConfig = Field(default_factory=AccessConfig)

    @property
    def charts_root(self) -> Path:
        return self.project_dir / "charts"


def load_config(path: str | Path) -> Config:
    config_path = Path(path).resolve()
    raw = yaml.safe_load(config_path.read_text()) or {}
    config = Config(**raw)

    base = config_path.parent
    if not config.project_dir.is_absolute():
        config.project_dir = (base / config.project_dir).resolve()
    if not config.storage.dir.is_absolute():
        config.storage.dir = (base / config.storage.dir).resolve()
    if config.render.query_cache and not config.render.query_cache.is_absolute():
        config.render.query_cache = (base / config.render.query_cache).resolve()
    return config
