"""list_dir / grep 单测：树形输出、忽略集、截断限额、正则边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mncc.safety import PathGuard
from mncc.tools import GrepTool, ListDirTool


@pytest.fixture()
def ls(tmp_path: Path):
    return ListDirTool(PathGuard(tmp_path))


@pytest.fixture()
def grep(tmp_path: Path):
    return GrepTool(PathGuard(tmp_path))


# ---- list_dir ----


def test_list_dir_tree_structure(ls, tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")
    out = ls.run(".")
    assert "src/" in out
    assert "main.py" in out
    assert "README.md" in out
    assert "├── " in out or "└── " in out  # 树形分支符号
    assert "src/" in out.splitlines()[0] or out.startswith("./")  # 根标签


def test_list_dir_ignores_defaults(ls, tmp_path: Path) -> None:
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "keep.py").write_text("x", encoding="utf-8")
    out = ls.run(".")
    assert "keep.py" in out
    assert "node_modules" not in out
    assert "__pycache__" not in out
    assert ".git" not in out


def test_list_dir_nonexistent_raises(ls, tmp_path: Path) -> None:
    with pytest.raises(Exception, match="不存在"):
        ls.run("nope")


def test_list_dir_file_raises(ls, tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(Exception, match="不是目录"):
        ls.run(str(f))


def test_list_dir_truncates_on_budget(ls, tmp_path: Path) -> None:
    for i in range(505):
        (tmp_path / f"f{i:03}.txt").write_text("x", encoding="utf-8")
    out = ls.run(".")
    assert "已截断" in out


def test_list_dir_empty(ls, tmp_path: Path) -> None:
    out = ls.run(".")
    assert "空目录" in out


def test_list_dir_symlink_dir_not_followed(ls, tmp_path: Path) -> None:
    import os

    real = tmp_path / "real"
    real.mkdir()
    (real / "secret.py").write_text("x", encoding="utf-8")
    try:
        os.symlink(real, tmp_path / "link", target_is_directory=True)
    except OSError:
        pytest.skip("当前环境无法创建符号链接")
    out = ls.run(".")
    assert "link/" in out  # 链接本身可见
    assert "secret.py" not in out  # 但不递归进入


# ---- grep ----


def test_grep_output_format(grep, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import os\nprint(os.getcwd())\n", encoding="utf-8")
    out = grep.run("os\\.getcwd", path=".")
    assert "a.py:2:print(os.getcwd())" in out


def test_grep_multiple_files_and_lines(grep, tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("foo()\nbar()\n", encoding="utf-8")
    (tmp_path / "y.py").write_text("foo()\n", encoding="utf-8")
    out = grep.run("foo", path=".")
    assert "x.py:1:foo()" in out
    assert "y.py:1:foo()" in out
    assert "bar" not in out


def test_grep_glob_filter(grep, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    out = grep.run("needle", path=".", glob="*.py")
    assert "a.py" in out
    assert "a.txt" not in out


def test_grep_match_cap_with_hint(grep, tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("hit\n" * 150, encoding="utf-8")
    out = grep.run("hit", path=".")
    # 100 条上限 + 提示
    assert out.count("big.py:") == 100
    assert "上限" in out


def test_grep_invalid_regex(grep, tmp_path: Path) -> None:
    with pytest.raises(Exception, match="正则"):
        grep.run("([a-z", path=".")


def test_grep_no_match(grep, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    assert "未找到匹配" in grep.run("nothing_here", path=".")


def test_grep_binary_skipped(grep, tmp_path: Path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"needle\x00\x01\x02")
    (tmp_path / "ok.py").write_text("needle\n", encoding="utf-8")
    out = grep.run("needle", path=".")
    assert "ok.py" in out
    assert "bin.dat" not in out


def test_grep_ignores_default_dirs(grep, tmp_path: Path) -> None:
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "lib.js").write_text("needle\n", encoding="utf-8")
    assert "未找到匹配" in grep.run("needle", path=".")


def test_grep_long_line_truncated(grep, tmp_path: Path) -> None:
    (tmp_path / "min.js").write_text("needle" + "x" * 500 + "\n", encoding="utf-8")
    out = grep.run("needle", path=".")
    assert "…" in out  # 超长行截断标记
    assert "x" * 400 not in out


def test_grep_relative_path_from_workspace_root(grep, tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.py").write_text("needle\n", encoding="utf-8")
    out = grep.run("needle", path="sub")
    assert "sub/a.py:1:needle" in out  # 相对工作区根，不带绝对前缀
