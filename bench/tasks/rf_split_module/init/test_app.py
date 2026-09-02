from app import App


def test_report_basic():
    text = "苹果,3.5\n香蕉,2\n# 注释行\n损坏行\n橙子,5.5\n"
    out = App(text).run()
    assert "总额: ¥11.00" in out
    assert "笔数: 3" in out
    assert "最大单笔: ¥5.50" in out


def test_amounts_below_min_ignored():
    out = App("小费,0.5\n正餐,20\n").run()
    assert "笔数: 1" in out
