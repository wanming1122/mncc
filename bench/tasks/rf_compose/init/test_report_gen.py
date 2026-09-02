from report_gen import process_file


def test_process_file(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("hello world\nfoo bar\n", encoding="utf-8")
    stats = process_file(str(p))
    assert stats == {"lines": 2, "chars": 20, "words": 4}
    report = (tmp_path / "note.txt.report").read_text(encoding="utf-8")
    assert "行数: 2" in report
    assert "字符数: 20" in report
    assert "单词数: 4" in report
