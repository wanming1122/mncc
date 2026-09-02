"""评分断言：注释剥离行为不变 + 空输入抛 MiniJsonError 而非裸异常。"""

import pytest
from mini_json import MiniJsonError, loads


def test_loads_plain_json():
    assert loads('{"a": 1}') == {"a": 1}


def test_line_comments_stripped():
    assert loads('{"a": 1} // 注释') == {"a": 1}


def test_hash_comments_stripped():
    assert loads('# 一行注释\n{"b": 2}') == {"b": 2}


def test_empty_input_friendly_error():
    with pytest.raises(MiniJsonError):
        loads("")


def test_comment_only_friendly_error():
    with pytest.raises(MiniJsonError):
        loads("// 只有注释")
