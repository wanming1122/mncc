"""重构评分：合并后行为等价（含历史差异）+ 包装函数确实是薄包装。"""

import inspect

from parse_utils import parse, parse_a, parse_b


def test_wrappers_preserve_behavior():
    assert parse_a("a, b") == ["A", "B"]
    assert parse_b("a, b") == ["a", "b"]
    assert parse_a(" a , b ,, c ") == ["A", "B", "C"]
    assert parse_b(" a , b ,, c ") == ["a", "b", "c"]


def test_shared_impl_matches_history():
    assert parse("a, b", upper=True) == ["A", "B"]
    assert parse("a, b") == ["a", "b"]


def test_error_cases_unchanged():
    assert parse_a(",,") == []
    assert parse_b(",,") == []
    assert parse_a("") == []
    assert parse_b("") == []


def test_wrappers_are_thin():
    # 包装函数本身不应再包含 split 逻辑（实现已合并）
    for func in (parse_a, parse_b):
        assert "split" not in inspect.getsource(func)
