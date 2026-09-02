"""评分断言：join_lines 的分隔符与空列表行为。"""

from utils import join_lines


def test_join_lines_basic():
    assert join_lines(["a", "b", "c"]) == "a, b, c"


def test_join_lines_custom_sep():
    assert join_lines(["x", "y"], "-") == "x-y"


def test_join_lines_empty():
    assert join_lines([]) == ""


def test_join_lines_single():
    assert join_lines(["only"]) == "only"
