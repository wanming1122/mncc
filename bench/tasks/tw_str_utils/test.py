"""元测试评分（D7）：agent 写的 test_utils.py 必须
(a) 对正确实现全绿；(b) 至少杀死一个变异体（证明测试不是空转）。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
TESTS = ROOT / "test_utils.py"

_ENV = {**os.environ, "PYTHONUTF8": "1"}  # Windows 子进程统一 UTF-8（§3）

# 变异体：reverse_words 对空串/纯空白返回 None 而不是空串
MUTANT_UTILS = '''"""字符串工具（含一个行为错误：空输入返回 None）。"""


def reverse_words(text):
    if not text.strip():
        return None
    return " ".join(reversed(text.split()))


def title_case(text):
    if not text:
        return ""
    return " ".join(w.capitalize() for w in text.split())


def count_vowels(text):
    return sum(1 for c in text.lower() if c in "aeiou")
'''


def _run_pytest(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_utils.py"],
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
        (work / "utils.py").write_text(MUTANT_UTILS, encoding="utf-8")
        (work / "test_utils.py").write_text(TESTS.read_text(encoding="utf-8"), encoding="utf-8")
        proc = _run_pytest(work)
        # 至少失败一次：证明测试真的在断言行为，而不是空转
        assert proc.returncode != 0, "测试没能杀死变异体（对错误实现也全绿）"
