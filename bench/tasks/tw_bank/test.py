"""元测试评分（D7）：agent 的 test_bank.py
(a) 对正确实现全绿；(b) 至少杀死一个变异体（证明测试不是空转）。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
TESTS = ROOT / "test_bank.py"

_ENV = {**os.environ, "PYTHONUTF8": "1"}  # Windows 子进程统一 UTF-8（§3）

# 变异体：withdraw 不检查余额，允许透支（余额可为负）
MUTANT_BANK = '''class Account:
    def __init__(self, owner):
        self.owner = owner
        self._balance = 0

    @property
    def balance(self):
        return self._balance

    def deposit(self, amount):
        if amount <= 0:
            raise ValueError("存款金额必须为正")
        self._balance += amount

    def withdraw(self, amount):
        if amount <= 0:
            raise ValueError("取款金额必须为正")
        self._balance -= amount  # 变异：漏掉余额检查
'''


def _run_pytest(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_bank.py"],
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
        (work / "bank.py").write_text(MUTANT_BANK, encoding="utf-8")
        (work / "test_bank.py").write_text(TESTS.read_text(encoding="utf-8"), encoding="utf-8")
        proc = _run_pytest(work)
        assert proc.returncode != 0, "测试没能杀死变异体（对错误实现也全绿）"
