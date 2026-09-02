"""评分断言（D2：跑 agent 时不拷入工作目录，评分阶段才解锁）。"""

import pytest
from calc import add, div, mul, sub


def test_add():
    assert add(2, 3) == 5


def test_sub():
    assert sub(5, 3) == 2
    assert sub(-1, -1) == 0


def test_mul():
    assert mul(4, 5) == 20


def test_div():
    assert div(10, 2) == 5


def test_div_by_zero_raises():
    with pytest.raises(ZeroDivisionError):
        div(1, 0)
