"""评分断言：LRU 命中/淘汰、等值 list 参数、不同函数缓存隔离。"""

from lru import lru_cache


def test_cache_hit_does_not_rerun():
    calls = []

    @lru_cache(4)
    def square(x):
        calls.append(x)
        return x * x

    assert square(3) == 9
    assert square(3) == 9
    assert len(calls) == 1


def test_distinct_functions_do_not_share_cache():
    @lru_cache(4)
    def double(x):
        return x * 2

    @lru_cache(4)
    def triple(x):
        return x * 3

    assert double(5) == 10
    assert triple(5) == 15
    assert double(5) == 10


def test_list_argument_supported_and_equal_to_tuple():
    calls = []

    @lru_cache(4)
    def total(items):
        calls.append(1)
        return sum(items)

    assert total([1, 2, 3]) == 6
    assert total([1, 2, 3]) == 6
    assert len(calls) == 1  # 等值 list 参数应命中缓存
    assert total((1, 2, 3)) == 6  # 元组与等值列表应命中同一缓存项
    assert len(calls) == 1


def test_eviction_respects_maxsize_lru():
    calls = []

    @lru_cache(2)
    def ident(x):
        calls.append(x)
        return x

    assert ident(1) == 1
    assert ident(2) == 2
    assert ident(1) == 1  # 1 回热（移到最近使用）
    assert ident(3) == 3  # 淘汰最久未用的 2
    assert ident(2) == 2  # 2 已不在缓存，重新计算
    assert len(calls) == 4
