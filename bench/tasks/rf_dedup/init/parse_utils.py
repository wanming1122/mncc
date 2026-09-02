"""两处近似重复的解析函数——重构对象，行为必须保持不变。"""


def parse_a(line):
    """解析 A 格式（旧代码）：按逗号拆分，结果转大写、过滤空段。"""
    parts = [p.strip() for p in line.split(",")]
    return [p.upper() for p in parts if p]


def parse_b(line):
    """解析 B 格式：与 parse_a 几乎一样，只差一处复制粘贴时漏掉的行为。"""
    parts = [p.strip() for p in line.split(",")]
    return [p for p in parts if p]  # 复制粘贴差异：没有转大写（历史行为，保留）
