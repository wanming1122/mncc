from geo import area, circle, distance, perimeter, point, rect, translate


def test_circle_area_perimeter():
    c = circle(2)
    assert round(area(c), 2) == 12.57
    assert round(perimeter(c), 2) == 12.57


def test_rect_area_perimeter():
    r = rect(3, 4)
    assert area(r) == 12
    assert perimeter(r) == 14


def test_translate_and_distance():
    p = point(1, 2)
    q = translate(p, 3, -1)
    assert distance(p, q) == 5
