"""评分断言：分页切片边界（含非整页的最后一页）。"""

from pager import paginate


def test_first_page():
    assert paginate(list(range(10)), 1, 4) == [0, 1, 2, 3]


def test_middle_page():
    assert paginate(list(range(10)), 2, 4) == [4, 5, 6, 7]


def test_last_page_partial():
    assert paginate(list(range(10)), 3, 4) == [8, 9]


def test_page_beyond_range():
    assert paginate([1, 2], 5, 2) == []
