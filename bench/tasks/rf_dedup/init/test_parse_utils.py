from parse_utils import parse_a, parse_b


def test_parse_a_uppercases_and_filters():
    assert parse_a(" a , b ,, c ") == ["A", "B", "C"]


def test_parse_b_keeps_case_and_filters():
    assert parse_b(" a , b ,, c ") == ["a", "b", "c"]


def test_empty_parts_filtered_out():
    assert parse_a(",,") == []
    assert parse_b(",,") == []
