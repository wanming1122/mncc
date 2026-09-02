"""评分断言：密码强度按长度 + 字符类别分档。"""

from password import strength


def test_short_is_weak():
    assert strength("Ab1!") == "weak"


def test_long_low_variety_is_weak():
    assert strength("abcdefgh") == "weak"  # 只有小写 1 类
    assert strength("abcdefg1") == "weak"  # 小写+数字 2 类


def test_three_categories_medium():
    assert strength("Abcdef12") == "medium"  # 小写+大写+数字


def test_four_categories_strong():
    assert strength("Abcdef1!") == "strong"


def test_empty_is_weak():
    assert strength("") == "weak"
