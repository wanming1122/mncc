"""评分断言：ISO 8601 duration 子集解析/格式化及互逆性。"""

import pytest
from timespan import format_duration, parse_duration


def test_parse_hours_minutes_seconds():
    assert parse_duration("PT1H30M") == 5400


def test_parse_single_unit():
    assert parse_duration("PT30M") == 1800
    assert parse_duration("PT45S") == 45
    assert parse_duration("PT1H") == 3600


def test_parse_hours_and_seconds():
    assert parse_duration("PT1H15S") == 3615


def test_parse_invalid():
    for bad in (
        "",
        "1H30M",
        "P1H",
        "PT",
        "PTH30M",
        "PT1H30M2",
        "PT1H1H",
        "PT-5M",
        "PT1.5H",
    ):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_format_full():
    assert format_duration(5400) == "PT1H30M"


def test_format_omits_zero_units():
    assert format_duration(1800) == "PT30M"
    assert format_duration(75) == "PT1M15S"
    assert format_duration(3600) == "PT1H"
    assert format_duration(0) == "PT0S"


def test_round_trip():
    for s in (5400, 3615, 45, 7500, 0):
        assert parse_duration(format_duration(s)) == s
