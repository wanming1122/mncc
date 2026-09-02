"""LRU 缓存装饰器（教学实现，行为待修复）。"""

_shared_cache: dict = {}
_shared_order: list = []


def lru_cache(maxsize=32):
    def decorator(func):
        def wrapper(*args):
            if args in _shared_cache:  # bug 1：list 参数不可哈希，抛 TypeError
                _shared_order.remove(args)
            else:
                _shared_cache[args] = func(*args)
            _shared_order.append(args)
            while len(_shared_order) > maxsize:
                _shared_cache.pop(_shared_order.pop(0))
            return _shared_cache[args]

        return wrapper

    return decorator
