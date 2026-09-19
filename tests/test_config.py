from pathlib import Path

import pytest
from pydantic import ValidationError

from dct_hub.config import load_config


def test_relative_paths_resolve_against_config_dir(tmp_path):
    config_file = tmp_path / "charts-tool.yml"
    config_file.write_text(
        "project_dir: ./proj\nstorage:\n  dir: .state\nrender:\n  query_cache: .state/qc.duckdb\n"
    )
    config = load_config(config_file)
    assert config.project_dir == (tmp_path / "proj").resolve()
    assert config.storage.dir == (tmp_path / ".state").resolve()
    assert config.render.query_cache == (tmp_path / ".state" / "qc.duckdb").resolve()
    assert config.charts_root == config.project_dir / "charts"


def test_defaults(tmp_path):
    config_file = tmp_path / "charts-tool.yml"
    config_file.write_text("project_dir: .\n")
    config = load_config(config_file)
    assert config.host == "127.0.0.1"
    assert config.port == 8080
    assert config.dct_bin == "dct"
    assert config.render.format == "html"
    assert config.render.query_cache is None
    assert config.policy.max_renders_per_minute == 30  # abuse cap is on by default


def test_warm_normalizes_string_entries(tmp_path):
    config_file = tmp_path / "charts-tool.yml"
    config_file.write_text(
        "project_dir: .\n"
        "warm:\n"
        "  boards:\n"
        "    - sales_daily\n"
        "    - board: exec/overview\n"
        "      vars:\n"
        "        - {region: East}\n"
    )
    config = load_config(config_file)
    assert [b.board for b in config.warm.boards] == ["sales_daily", "exec/overview"]
    assert config.warm.boards[0].vars == [{}]
    assert config.warm.boards[1].vars == [{"region": "East"}]


def test_warm_defaults_off(tmp_path):
    config_file = tmp_path / "charts-tool.yml"
    config_file.write_text("project_dir: .\n")
    assert load_config(config_file).warm.boards == []


def test_warm_board_rejects_unknown_keys(tmp_path):
    config_file = tmp_path / "charts-tool.yml"
    config_file.write_text("project_dir: .\nwarm:\n  boards:\n    - {board: x, bogus: 1}\n")
    with pytest.raises(ValidationError):
        load_config(config_file)


def test_ha_requires_s3_bucket_with_postgres(tmp_path):
    config_file = tmp_path / "charts-tool.yml"
    config_file.write_text("project_dir: .\nstorage:\n  postgres: postgresql://hub/db\n")
    with pytest.raises(ValidationError, match="s3.bucket"):
        load_config(config_file)


def test_ha_rejects_s3_without_postgres(tmp_path):
    config_file = tmp_path / "charts-tool.yml"
    config_file.write_text("project_dir: .\nstorage:\n  s3:\n    bucket: artifacts\n")
    with pytest.raises(ValidationError, match="postgres"):
        load_config(config_file)


def test_ha_valid_config_and_env_dsn(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PG_DSN", "postgresql://hub:secret@pg/db")
    config_file = tmp_path / "charts-tool.yml"
    config_file.write_text(
        "project_dir: .\n"
        "storage:\n"
        "  postgres: ${TEST_PG_DSN}\n"
        "  s3:\n"
        "    bucket: artifacts\n"
        "    endpoint_url: http://minio:9000\n"
    )
    config = load_config(config_file)
    assert config.storage.postgres == "postgresql://hub:secret@pg/db"
    assert config.storage.s3.bucket == "artifacts"
    assert config.storage.s3.endpoint_url == "http://minio:9000"
    assert config.storage.s3.prefix == "artifacts/"
