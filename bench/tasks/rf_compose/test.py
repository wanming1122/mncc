"""重构评分：纯函数与 IO 分离 + 行为等价。"""

from report_gen import compute_stats, process_file, transform


def test_transform_is_pure(monkeypatch):
    text = "hello world\nfoo bar\n"

    def boom(*args, **kwargs):
        raise AssertionError("transform 不应做文件 IO")

    monkeypatch.setattr("builtins.open", boom)
    report = transform(text, source="note.txt")
    assert "报告：note.txt" in report
    assert "行数: 2" in report
    assert "单词数: 4" in report


def test_compute_stats_matches_spec():
    assert compute_stats("a b\nc\n") == {"lines": 2, "chars": 6, "words": 3}
    assert compute_stats("") == {"lines": 0, "chars": 0, "words": 0}


def test_process_file_composes_same_output(tmp_path):
    p = tmp_path / "n.txt"
    p.write_text("a b\nc\n", encoding="utf-8")
    stats = process_file(str(p))
    assert stats == compute_stats("a b\nc\n")
    assert (tmp_path / "n.txt.report").exists()
