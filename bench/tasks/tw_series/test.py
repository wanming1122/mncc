"""元测试评分（D7）：agent 的 test_series.py
(a) 对正确实现全绿；(b) 至少杀死一个变异体（证明测试不是空转）。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
TESTS = ROOT / "test_series.py"

_ENV = {**os.environ, "PYTHONUTF8": "1"}  # Windows 子进程统一 UTF-8（§3）

# 变异体：geometric_sum 漏掉 r == 1 特例（r=1 时除零）
MUTANT_SERIES = """def arithmetic_sum(n):
    if n < 0:
        raise ValueError("n 必须为非负整数")
    return n * (n + 1) // 2


def geometric_sum(a, r, n):
    if n < 0:
        raise ValueError("n 必须为非负整数")
    if n == 0:
        return 0.0
    return a * (1 - r**n) / (1 - r)  # 变异：漏掉 r==1 特例
"""


def _run_pytest(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_series.py"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_ENV,
    )


def test_written_tests_exist():
    assert TESTS.is_file() and "def test_" in TESTS.read_text(encoding="utf-8")


def test_written_tests_pass_on_correct_impl():
    proc = _run_pytest(ROOT)
    assert proc.returncode == 0, f"对正确实现的测试应全绿：\n{proc.stdout}\n{proc.stderr}"


def test_written_tests_kill_mutant():
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        (work / "series.py").write_text(MUTANT_SERIES, encoding="utf-8")
        (work / "test_series.py").write_text(TESTS.read_text(encoding="utf-8"), encoding="utf-8")
        proc = _run_pytest(work)
        assert proc.returncode != 0, "测试没能杀死变异体（对错误实现也全绿）"
