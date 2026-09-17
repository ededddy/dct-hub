"""Loads charts-tool.yml. Relative paths resolve against the config file's directory."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dir: Path = Path(".hub")


class RenderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = "html"
    timeout_s: int = 120
    query_cache: Path | None = None


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_dir: Path
    host: str = "127.0.0.1"
    port: int = 8080
    dct_bin: str = "dct"
    storage: StorageConfig = Field(default_factory=StorageConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)

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
