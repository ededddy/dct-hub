import pytest

from dct_hub.boards import (
    BoardNotFoundError,
    InvalidBoardRef,
    list_boards,
    normalize_board,
    resolve_board_file,
)


def test_normalize_accepts_common_forms():
    assert normalize_board("sales_daily") == "sales_daily"
    assert normalize_board("sales_daily.yml") == "sales_daily"
    assert normalize_board("charts/sales_daily.yml") == "sales_daily"
    assert normalize_board("/charts/folder/board.yaml") == "folder/board"


def test_normalize_rejects_traversal_and_empty():
    with pytest.raises(InvalidBoardRef):
        normalize_board("../secrets")
    with pytest.raises(InvalidBoardRef):
        normalize_board("charts/../../etc/passwd")
    with pytest.raises(InvalidBoardRef):
        normalize_board("")


def test_resolve_board_file(tmp_path):
    charts = tmp_path / "charts"
    charts.mkdir()
    (charts / "a.yml").write_text("title: A\n")
    assert resolve_board_file(charts, "a") == charts / "a.yml"
    with pytest.raises(BoardNotFoundError):
        resolve_board_file(charts, "missing")


def test_list_boards_skips_meta_and_partials(tmp_path):
    charts = tmp_path / "charts"
    (charts / "partials").mkdir(parents=True)
    (charts / "meta.yml").write_text("theme: paper\n")
    (charts / "a.yml").write_text("title: Board A\nnotes: first\n")
    (charts / "partials" / "shared.yml").write_text("title: Shared\n")

    boards = list_boards(charts)
    assert [(b.board, b.title, b.notes) for b in boards] == [("a", "Board A", "first")]
