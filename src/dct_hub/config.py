"""Loads charts-tool.yml. Relative paths resolve against the config file's directory."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .auth.config import AccessConfig, AuthConfig


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dir: Path = Path(".hub")


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


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_dir: Path
    host: str = "127.0.0.1"
    port: int = 8080
    dct_bin: str = "dct"
    storage: StorageConfig = Field(default_factory=StorageConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
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
