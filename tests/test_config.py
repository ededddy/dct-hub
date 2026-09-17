from pathlib import Path

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
