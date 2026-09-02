"""评分断言：column_stats 对任意数值 CSV 返回每列 mean/min/max，非数值列跳过。"""

import tempfile
from pathlib import Path

import pytest
from stats import column_stats


def _write_csv(text: str) -> Path:
    p = Path(tempfile.mkdtemp()) / "data.csv"
    p.write_text(text, encoding="utf-8")
    return p


def test_basic_columns():
    p = _write_csv("price,qty\n10,2\n20,4\n30,6\n")
    got = column_stats(str(p))
    assert got["price"] == {"mean": 20.0, "min": 10.0, "max": 30.0}
    assert got["qty"] == {"mean": 4.0, "min": 2.0, "max": 6.0}


def test_non_numeric_column_skipped():
    p = _write_csv("name,score\nalice,80\nbob,90\n")
    got = column_stats(str(p))
    assert "name" not in got
    assert got["score"] == {"mean": 85.0, "min": 80.0, "max": 90.0}


def test_mixed_column_skipped():
    p = _write_csv("a\n1\nx\n2\n")
    got = column_stats(str(p))
    assert got == {}


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        column_stats("nope.csv")
