from dct_hub.cachekey import artifact_key, board_fingerprint


def test_vars_order_does_not_change_key():
    a = artifact_key("sales_daily", {"region": "West", "day": "2026-09-17"}, "fp", "html", "0.8.0")
    b = artifact_key("sales_daily", {"day": "2026-09-17", "region": "West"}, "fp", "html", "0.8.0")
    assert a == b


def test_key_changes_with_inputs():
    base = artifact_key("sales_daily", {}, "fp", "html", "0.8.0")
    assert base != artifact_key("sales_daily", {"region": "West"}, "fp", "html", "0.8.0")
    assert base != artifact_key("sales_daily", {}, "other-fp", "html", "0.8.0")
    assert base != artifact_key("sales_daily", {}, "fp", "svg", "0.8.0")
    assert base != artifact_key("sales_daily", {}, "fp", "html", "0.9.0")
    assert base != artifact_key("other_board", {}, "fp", "html", "0.8.0")


def test_fingerprint_changes_with_board_and_meta_chain(tmp_path):
    charts = tmp_path / "charts"
    (charts / "folder").mkdir(parents=True)
    (charts / "meta.yml").write_text("theme: paper\n")
    board = charts / "folder" / "b.yml"
    board.write_text("title: B\n")

    fp = board_fingerprint(charts, board)
    (charts / "meta.yml").write_text("theme: neon\n")
    assert board_fingerprint(charts, board) != fp

    fp = board_fingerprint(charts, board)
    (charts / "folder" / "meta.yml").write_text("theme: vivid\n")
    assert board_fingerprint(charts, board) != fp

    fp = board_fingerprint(charts, board)
    board.write_text("title: B2\n")
    assert board_fingerprint(charts, board) != fp


def test_fingerprint_changes_with_project_config(tmp_path):
    charts = tmp_path / "charts"
    charts.mkdir()
    board = charts / "b.yml"
    board.write_text("title: B\n")
    project_config = tmp_path / "dbt_charts.yml"
    project_config.write_text("sources: {}\n")

    fp = board_fingerprint(charts, board, project_config)
    project_config.write_text("sources: {}\ntheme: neon\n")
    assert board_fingerprint(charts, board, project_config) != fp

    assert board_fingerprint(charts, board) != board_fingerprint(charts, board, project_config)
