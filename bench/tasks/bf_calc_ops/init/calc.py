def add(a, b):
    return a + b


def sub(a, b):
    return a + b  # bug 1：减法写成了加法


def mul(a, b):
    return a * b


def div(a, b):
    if b == 0:
        return 0  # bug 2：除零没有抛 ZeroDivisionError
    return a / b
