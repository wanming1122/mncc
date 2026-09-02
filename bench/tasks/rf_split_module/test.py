"""重构评分（D7）：行为等价（扩展用例）+ 结构真的拆开了。"""

import importlib
from pathlib import Path

from app import (  # noqa: F401  # 这行 import 本身就是断言：re-export 缺失即收集失败
    App,
    parse_lines,
    render_report,
    summarize,
)


def test_behavior_equivalent_extended():
    text = "苹果,3.5\n香蕉,2\n橙子,5.5\n橘子,1\n坏行\n太小,0.4\n"
    out = App(text).run()
    assert "总额: ¥12.00" in out
    assert "笔数: 4" in out


def test_app_py_is_thin():
    import app as app_module

    content = Path(app_module.__file__).read_text(encoding="utf-8")
    # 实现搬走后 app.py 只应剩接口：不允许还藏着 parse_lines 的函数体
    assert "def parse_lines" not in content


def test_modules_exist_and_behavior_intact():
    config = importlib.import_module("config")
    data = importlib.import_module("data")
    fmt = importlib.import_module("format")
    assert config.normalize_amount("1.234") == 1.23
    assert data.parse_lines("a,1\nb,2\n") == [
        {"name": "a", "amount": 1.0},
        {"name": "b", "amount": 2.0},
    ]
    assert "总额" in fmt.render_report(data.summarize(data.parse_lines("a,1\n")))
