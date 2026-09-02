"""read_file / write_file / edit_file 单测（文件系统操作，全部用 tmp_path 隔离）。

M3 起工具构造注入 PathGuard(tmp_path)：每项测试的工作区即 tmp_path。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mncc.safety import PathGuard, SafetyViolation
from mncc.tools.files import MAX_LINES, EditFileTool, ReadFileTool, WriteFileTool


@pytest.fixture()
def read(tmp_path: Path):
    return ReadFileTool(PathGuard(tmp_path))


@pytest.fixture()
def write(tmp_path: Path):
    return WriteFileTool(PathGuard(tmp_path))


@pytest.fixture()
def edit(tmp_path: Path):
    return EditFileTool(PathGuard(tmp_path))


# ---- read_file ----


def test_read_returns_numbered_lines(read, tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")
    out = read.run(path=str(f))
    assert "1\tline1" in out
    assert "2\tline2" in out
    assert "3\tline3" in out


def test_read_offset_and_limit(read, tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"L{i}" for i in range(1, 101)) + "\n", encoding="utf-8")
    out = read.run(path=str(f), offset=10, limit=5)
    assert "10\tL10" in out
    assert "14\tL14" in out
    assert "L9" not in out  # offset=10 跳过前面的行
    assert "15\tL15" not in out  # limit=5 只取 5 行
    # 续读提示
    assert "offset=15" in out


def test_read_offset_only(read, tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"L{i}" for i in range(1, 51)) + "\n", encoding="utf-8")
    out = read.run(path=str(f), offset=48, limit=3)
    assert "48\tL48" in out
    assert "49\tL49" in out
    assert "50\tL50" in out


def test_read_long_line_truncated(read, tmp_path: Path) -> None:
    f = tmp_path / "minified.js"
    f.write_text("x" * 5000 + "\n", encoding="utf-8")
    out = read.run(path=str(f))
    assert "已截断" in out
    assert "x" * 2500 not in out  # 确实被截了（2000 以上被截断）


def test_read_nonexistent_raises_tool_error(read, tmp_path: Path) -> None:
    with pytest.raises(Exception, match="文件不存在"):
        read.run(path=str(tmp_path / "nope.txt"))


def test_read_outside_workspace_rejected(read, tmp_path: Path) -> None:
    """界外路径先于存在性检查被守卫拒绝（M3：路径守卫在最前）。"""
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(SafetyViolation, match="越界"):
        read.run(path=str(outside))


def test_read_directory_raises_tool_error(read, tmp_path: Path) -> None:
    with pytest.raises(Exception, match="目录"):
        read.run(path=str(tmp_path))


def test_read_empty_file(read, tmp_path: Path) -> None:
    f = tmp_path / "empty.py"
    f.write_text("", encoding="utf-8")
    assert read.run(path=str(f)) == "（空文件）"


def test_read_offset_beyond_end_raises(read, tmp_path: Path) -> None:
    f = tmp_path / "short.txt"
    f.write_text("line1\nline2\n", encoding="utf-8")
    with pytest.raises(Exception, match="超出文件总行数"):
        read.run(path=str(f), offset=10)


def test_read_zero_offset_raises(read, tmp_path: Path) -> None:
    f = tmp_path / "s.txt"
    f.write_text("x\n", encoding="utf-8")
    with pytest.raises(Exception, match="offset 从 1 开始"):
        read.run(path=str(f), offset=0)


def test_read_lots_of_lines_pagination_hint(read, tmp_path: Path) -> None:
    f = tmp_path / "huge.txt"
    f.write_text("\n".join(f"L{i}" for i in range(MAX_LINES + 100)) + "\n", encoding="utf-8")
    out = read.run(path=str(f))
    assert "继续读取" in out
    assert f"共 {MAX_LINES + 100} 行" in out
    assert f"第 1-{MAX_LINES} 行" in out


def test_read_brief(read) -> None:
    assert read.brief({"path": "my/file.py"}) == "读取 my/file.py"


# ---- write_file ----


def test_write_creates_file(write, tmp_path: Path) -> None:
    f = tmp_path / "new.py"
    result = write.run(path=str(f), content="print('hi')\n")
    assert f.read_text(encoding="utf-8") == "print('hi')\n"
    assert "已写入" in result
    assert "1 行" in result


def test_write_creates_parent_directories(write, tmp_path: Path) -> None:
    f = tmp_path / "a" / "b" / "c.py"
    write.run(path=str(f), content="# deep\n")
    assert f.read_text(encoding="utf-8") == "# deep\n"


def test_write_overwrites_existing(write, tmp_path: Path) -> None:
    f = tmp_path / "old.txt"
    f.write_text("old content", encoding="utf-8")
    write.run(path=str(f), content="new content")
    assert f.read_text(encoding="utf-8") == "new content"


def test_write_lf_newline_enforced(write, tmp_path: Path) -> None:
    """Windows 下 \r\n 会混入 diff，验证强制 LF。"""
    f = tmp_path / "lf.txt"
    write.run(path=str(f), content="a\nb\n")
    raw = f.read_bytes()
    assert b"\r\n" not in raw


def test_write_needs_confirm(write) -> None:
    assert write.needs_confirm({}) is True


def test_write_preview_truncates_long_content(write) -> None:
    content = "line\n" * 100
    preview = write.preview({"path": "x.py", "content": content})
    assert "共 100 行" in preview
    assert "x.py" in preview


def test_write_empty_content(write, tmp_path: Path) -> None:
    f = tmp_path / "empty.txt"
    result = write.run(path=str(f), content="")
    assert "0 字符" in result


def test_write_io_error_raises_tool_error(write, tmp_path: Path) -> None:
    """路径到一个已有目录上，write_text 会报 IsADirectoryError。"""
    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(Exception, match="写入失败"):
        write.run(path=str(d), content="x")


def test_write_outside_workspace_rejected(write, tmp_path: Path) -> None:
    with pytest.raises(SafetyViolation, match="越界"):
        write.run(path=str(tmp_path.parent / "escape.txt"), content="x")


def test_write_traversal_rejected(write, tmp_path: Path) -> None:
    with pytest.raises(SafetyViolation, match="越界"):
        write.run(path="sub/../../escape.txt", content="x")


# ---- edit_file ----


def test_edit_replaces_unique_match(edit, tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")
    out = edit.run(path=str(f), old_string="x = 1", new_string="x = 42")
    assert f.read_text(encoding="utf-8") == "x = 42\ny = 2\n"
    assert "第 1 行" in out
    assert "已修改" in out


def test_edit_multiple_matches_listed(edit, tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("a = f(1)\nb = f(2)\nc = f(3)\n", encoding="utf-8")
    with pytest.raises(Exception, match="命中 3 处"):
        edit.run(path=str(f), old_string="f(", new_string="g(")
    # 文件未被改动
    assert f.read_text(encoding="utf-8") == "a = f(1)\nb = f(2)\nc = f(3)\n"


def test_edit_miss_returns_nearest_snippet(edit, tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    with pytest.raises(Exception, match="0 次命中") as exc_info:
        edit.run(path=str(f), old_string="return a - b", new_string="return a + b")
    # 诊断包含实际内容与差异对比，帮助模型自纠
    assert "return a + b" in str(exc_info.value)
    assert "差异" in str(exc_info.value)


def test_edit_trailing_whitespace_hint_not_auto_fixed(edit, tmp_path: Path) -> None:
    """old_string 带行尾空白而文件没有：提示空白差异，只提示不自动改（决策 4）。"""
    f = tmp_path / "code.py"
    f.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(Exception, match="行尾空白"):
        edit.run(path=str(f), old_string="x = 1  ", new_string="x = 2")
    assert f.read_text(encoding="utf-8") == "x = 1\n"  # 原样未动


def test_edit_nonexistent_file_suggests_write(edit, tmp_path: Path) -> None:
    with pytest.raises(Exception, match="write_file"):
        edit.run(path=str(tmp_path / "new.py"), old_string="a", new_string="b")


def test_edit_binary_file_rejected(edit, tmp_path: Path) -> None:
    f = tmp_path / "bin.dat"
    f.write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(Exception, match="二进制"):
        edit.run(path=str(f), old_string="a", new_string="b")


def test_edit_old_equals_new_rejected(edit, tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(Exception, match="相同"):
        edit.run(path=str(f), old_string="x = 1", new_string="x = 1")


def test_edit_crlf_preserved(edit, tmp_path: Path) -> None:
    f = tmp_path / "win.py"
    f.write_bytes(b"a = 1\r\nb = 2\r\n")
    edit.run(path=str(f), old_string="a = 1", new_string="a = 10")
    assert f.read_bytes() == b"a = 10\r\nb = 2\r\n"  # 主导行尾 CRLF 还原


def test_edit_lf_preserved(edit, tmp_path: Path) -> None:
    f = tmp_path / "unix.py"
    f.write_bytes(b"a = 1\nb = 2\n")
    edit.run(path=str(f), old_string="b = 2", new_string="b = 20\nc = 30")
    assert f.read_bytes() == b"a = 1\nb = 20\nc = 30\n"


def test_edit_no_trailing_newline_preserved(edit, tmp_path: Path) -> None:
    f = tmp_path / "noeol.py"
    f.write_bytes(b"x = 1\ny = 2")
    edit.run(path=str(f), old_string="x = 1", new_string="x = 3")
    assert f.read_bytes() == b"x = 3\ny = 2"


def test_edit_needs_confirm_and_diff_preview(edit, tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")
    assert edit.needs_confirm({}) is True
    assert edit.preview_lexer == "diff"
    preview = edit.preview({"path": str(f), "old_string": "x = 1", "new_string": "x = 9"})
    assert "-x = 1" in preview
    assert "+x = 9" in preview


def test_edit_preview_on_missing_match_raises(edit, tmp_path: Path) -> None:
    """预览失败照实抛错（registry 层转 is_error，D7 场景；注定失败不该询问用户）。"""
    f = tmp_path / "code.py"
    f.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(Exception, match="0 次命中"):
        edit.preview({"path": str(f), "old_string": "nope", "new_string": "x"})


def test_edit_outside_workspace_rejected(edit, tmp_path: Path) -> None:
    with pytest.raises(SafetyViolation, match="越界"):
        edit.run(path=str(tmp_path.parent / "x.py"), old_string="a", new_string="b")
