"""评分断言：以子进程方式驱动 todo.py 的完整增/查/完成/删除行为。"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent

# Windows 子进程默认 GBK 输出（§3）：强制 UTF-8，避免中文断言被乱码误判
_ENV = {**os.environ, "PYTHONUTF8": "1"}


@pytest.fixture()
def work(tmp_path: Path) -> Path:
    shutil.copy(ROOT / "todo.py", tmp_path / "todo.py")
    return tmp_path


def run_cli(work: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "todo.py", *args],
        cwd=str(work),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_ENV,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def test_add_list_done_lifecycle(work: Path) -> None:
    rc, out = run_cli(work, "add", "写周报")
    assert rc == 0 and out == "added: 1: 写周报"
    rc, out = run_cli(work, "add", "买牛奶")
    assert rc == 0 and out == "added: 2: 买牛奶"
    rc, out = run_cli(work, "list")
    assert rc == 0 and out == "1: 写周报\n2: 买牛奶"
    rc, out = run_cli(work, "done", "1")
    assert rc == 0 and out == "done: 1"
    rc, out = run_cli(work, "list")
    assert rc == 0 and out == "2: 买牛奶"


def test_remove_and_not_found(work: Path) -> None:
    run_cli(work, "add", "任务")
    rc, out = run_cli(work, "remove", "1")
    assert out == "removed: 1"
    rc, out = run_cli(work, "remove", "1")
    assert out == "not found: 1"
    rc, out = run_cli(work, "done", "99")
    assert out == "not found: 99"


def test_todos_json_structure(work: Path) -> None:
    run_cli(work, "add", "任务一")
    data = json.loads((work / "todos.json").read_text(encoding="utf-8"))
    assert data == [{"id": 1, "text": "任务一", "done": False}]


def test_unknown_command_usage_exit_2(work: Path) -> None:
    rc, out = run_cli(work, "frobnicate")
    assert rc == 2 and "usage" in out.lower()
