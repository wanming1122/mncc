"""日期解析器（实现正确，任务是为它写测试，不要修改本文件）。"""

import datetime


def parse_date(text):
    """解析 YYYY-MM-DD 字符串，返回 datetime.date；非法输入抛 ValueError。"""
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise ValueError(f"日期格式错误：{text!r}（应为 YYYY-MM-DD）")
    year, month, day = (int(p) for p in text.split("-"))
    return datetime.date(year, month, day)  # 非法月/日（如 2 月 30 日）抛 ValueError
