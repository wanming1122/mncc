"""评分断言：日期升序 + 同日期按 name 升序 + 稳定性。"""

from records import sort_records


def _records():
    return [
        {"date": "2026-01-02", "name": "beta"},
        {"date": "2025-12-31", "name": "alpha"},
        {"date": "2026-01-02", "name": "alpha"},
        {"date": "2026-01-01", "name": "gamma"},
    ]


def test_sort_by_date_then_name():
    got = [r["name"] for r in sort_records(_records())]
    assert got == ["alpha", "gamma", "alpha", "beta"]


def test_equal_keys_keep_original_order():
    recs = [
        {"date": "2026-01-01", "name": "x"},
        {"date": "2026-01-01", "name": "x"},
    ]
    assert sort_records(recs) == recs


def test_date_not_string_compare():
    # 月份跨年的边界：2026-10 应排在 2025-11 之后（字符串比较会排错）
    recs = [
        {"date": "2026-10-05", "name": "a"},
        {"date": "2025-11-30", "name": "b"},
    ]
    got = [r["name"] for r in sort_records(recs)]
    assert got == ["b", "a"]
