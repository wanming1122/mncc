"""重构评分：内部结构 dataclass 化 + 行为保持（扩展用例）。"""

import dataclasses

import geo
from geo import circle, distance, point, rect, translate


def test_types_are_dataclasses():
    assert dataclasses.is_dataclass(geo.Point)
    assert dataclasses.is_dataclass(geo.Circle)
    assert dataclasses.is_dataclass(geo.Rect)


def test_factories_return_dataclass_instances():
    assert isinstance(circle(1), geo.Circle)
    assert isinstance(rect(1, 2), geo.Rect)
    assert isinstance(point(1, 2), geo.Point)


def test_behavior_extended():
    c = circle(3)
    assert round(geo.area(c), 4) == round(3.141592653589793 * 9, 4)
    p1, p2 = point(0, 0), point(6, 8)
    assert distance(p1, p2) == 10
    q = translate(p1, 5, 5)
    assert (q.x, q.y) == (5, 5)
    r = rect(2.5, 4)
    assert geo.area(r) == 10.0
    assert geo.perimeter(r) == 13.0
