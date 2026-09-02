"""数值级数（实现正确，任务是为它写测试，不要修改本文件）。"""


def arithmetic_sum(n):
    """1 + 2 + ... + n。"""
    if n < 0:
        raise ValueError("n 必须为非负整数")
    return n * (n + 1) // 2


def geometric_sum(a, r, n):
    """等比数列前 n 项和：a + a*r + ... + a*r**(n-1)。r == 1 时等于 a*n。"""
    if n < 0:
        raise ValueError("n 必须为非负整数")
    if n == 0:
        return 0.0
    if r == 1:
        return a * n
    return a * (1 - r**n) / (1 - r)
