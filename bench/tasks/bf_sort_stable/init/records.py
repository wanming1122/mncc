"""按 (日期, 名称) 双键排序的交易记录。"""


def sort_records(records):
    def key(r):
        y, m, d = r["date"].split("-")
        return (m, d, y, r["name"])  # bug：月份当第一键，12 月记录排到 1 月后面

    return sorted(records, key=key)
