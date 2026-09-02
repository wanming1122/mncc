"""订单报表：读取文本流水，输出汇总报表。

结构按 配置 / 数据解析 / 报表输出 三段组织——本模块是重构任务的对象。
"""

CONFIG = {
    "min_amount": 1.0,
    "currency": "¥",
}


def normalize_amount(value):
    """把金额统一为 float；非法输入返回 0.0。"""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(amount, 2)


def is_valid_line(line):
    return bool(line and not line.startswith("#"))


def parse_lines(text):
    """解析流水文本（每行 "商品,金额"），返回记录列表。"""
    records = []
    for line in text.splitlines():
        if not is_valid_line(line):
            continue
        parts = line.split(",", 1)
        if len(parts) != 2:
            continue
        name, amount_text = parts
        amount = normalize_amount(amount_text.strip())
        if amount < CONFIG["min_amount"]:
            continue
        records.append({"name": name.strip(), "amount": amount})
    return records


def summarize(records):
    """汇总记录：总额 + 笔数 + 最大单笔。"""
    total = sum(r["amount"] for r in records)
    count = len(records)
    max_amount = max((r["amount"] for r in records), default=0.0)
    return {"total": round(total, 2), "count": count, "max": max_amount}


def render_report(summary):
    """把汇总渲染成文本报表。"""
    lines = [
        "订单报表",
        "=" * 8,
        f"总额: {CONFIG['currency']}{summary['total']:.2f}",
        f"笔数: {summary['count']}",
        f"最大单笔: {CONFIG['currency']}{summary['max']:.2f}",
    ]
    return "\n".join(lines)


class App:
    """对外入口：parse + summarize + render 的组合。"""

    def __init__(self, text=""):
        self.text = text

    def run(self):
        records = parse_lines(self.text)
        return render_report(summarize(records))
