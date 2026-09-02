"""评分断言：slug 生成的大小写、非 ASCII 移除、连字符合并与修剪。"""

from slugify import slugify


def test_basic_lowercase():
    assert slugify("Hello World") == "hello-world"


def test_punctuation_to_hyphen():
    assert slugify("foo_bar.baz") == "foo-bar-baz"


def test_chinese_removed():
    assert slugify("你好hello世界") == "hello"


def test_whitespace_and_symbols_only():
    assert slugify("  !!!  ") == ""


def test_repeated_hyphens_collapsed():
    assert slugify("a  -  b") == "a-b"


def test_leading_trailing_hyphens_stripped():
    assert slugify("-abc-") == "abc"


def test_empty():
    assert slugify("") == ""


def test_digits_kept():
    assert slugify("Version 2.0") == "version-2-0"
