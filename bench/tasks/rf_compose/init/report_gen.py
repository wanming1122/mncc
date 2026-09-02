"""文本报告生成：当前把 读文件/处理/写文件 混在一个函数里（重构对象）。"""


def process_file(path):
    """读入文本文件，统计行数/字符数/单词数，追加到报告文件。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines()
    stats = {
        "lines": len(lines),
        "chars": len(text),
        "words": len(text.split()),
    }
    report = (
        f"报告：{path}\n"
        f"行数: {stats['lines']}\n"
        f"字符数: {stats['chars']}\n"
        f"单词数: {stats['words']}\n"
    )
    with open(path + ".report", "w", encoding="utf-8") as f:
        f.write(report)
    return stats
