"""元测试评分（D7）：agent 的 test_date_parser.py
(a) 对正确实现全绿；(b) 至少杀死一个变异体（证明测试不是空转）。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
TESTS = ROOT / "test_date_parser.py"

_ENV = {**os.environ, "PYTHONUTF8": "1"}  # Windows 子进程统一 UTF-8（§3）

# 变异体：解析出的"日"恒为 1（格式与月校验都保留）
MUTANT_DATE_PARSER = '''"""日期解析器（含一个行为错误：日恒为 1）。"""

import datetime


def parse_date(text):
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise ValueError(f"日期格式错误：{text!r}（应为 YYYY-MM-DD）")
    year, month, day = (int(p) for p in text.split("-"))
    return datetime.date(year, month, 1)  # 变异：日子恒为 1
'''


def _run_pytest(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_date_parser.py"],
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
        (work / "date_parser.py").write_text(MUTANT_DATE_PARSER, encoding="utf-8")
        (work / "test_date_parser.py").write_text(
            TESTS.read_text(encoding="utf-8"), encoding="utf-8"
        )
        proc = _run_pytest(work)
        assert proc.returncode != 0, "测试没能杀死变异体（对错误实现也全绿）"
