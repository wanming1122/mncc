"""run_command 单测：UTF-8、超时强杀、输出截断；M3 起覆盖命令守卫集成。"""

from __future__ import annotations

import pytest

from mncc.safety import CommandGuard
from mncc.tools.command import RunCommandTool


@pytest.fixture()
def cmd():
    return RunCommandTool(CommandGuard())


def test_stdout_and_exit_code(cmd) -> None:
    out = cmd.run("echo hello", timeout=5)
    assert "exit_code: 0" in out
    assert "hello" in out


def test_stderr_captured(cmd) -> None:
    # Python 一行脚本写 stderr
    out = cmd.run('python -c "import sys; sys.stderr.write(\'err_msg\')"', timeout=10)
    assert "err_msg" in out
    assert "stderr" in out


def test_nonzero_exit_code_is_not_error(cmd) -> None:
    """非零退出码不算 is_error——exit_code 本身就是给模型的事实。"""
    out = cmd.run("python -c \"raise SystemExit(42)\"", timeout=10)
    assert "exit_code: 42" in out


def test_timeout_kills_process(cmd) -> None:
    with pytest.raises(Exception, match="超过.*未结束"):
        cmd.run("python -c \"import time; time.sleep(60)\"", timeout=1)


def test_timeout_shows_partial_output(cmd) -> None:
    # flush=True：print 到管道默认块缓冲，1s 内不 flush 就看不到部分输出
    script = "python -c \"import time; print('partial', flush=True); time.sleep(60)\""
    with pytest.raises(Exception, match="已产生"):
        cmd.run(script, timeout=1)


def test_timeout_clamped_to_max(cmd) -> None:
    """timeout 超过 MAX_TIMEOUT 时静默钳制，不报错。"""
    # 只验证不抛异常；实际超时仍是 MAX_TIMEOUT 而不是 99999
    out = cmd.run("echo ok", timeout=99999)
    assert "ok" in out


def test_invalid_timeout_raises(cmd) -> None:
    with pytest.raises(Exception, match="timeout"):
        cmd.run("echo ok", timeout="not_a_number")  # type: ignore[arg-type]


def test_output_truncation_preserves_head_and_tail(cmd) -> None:
    # 产生超长 stdout，验证截断后保留首尾
    script = 'python -c "print(\'H\'); print(\'x\' * 20000); print(\'T\')"'
    out = cmd.run(script, timeout=30)
    assert "H" in out  # 头保留
    assert "T" in out  # 尾保留
    assert "中间省略" in out  # 被截断


def test_no_output_section_when_empty(cmd) -> None:
    # python -c "pass" 跨平台且不产生任何输出（Unix 的 true 在 Windows cmd 下不存在）
    out = cmd.run('python -c "pass"', timeout=10)
    assert "exit_code: 0" in out
    assert "无输出" in out


def test_utf8_output_no_garbled(cmd) -> None:
    out = cmd.run('python -c "print(\'你好世界\')"', timeout=10)
    assert "你好世界" in out


def test_brief(cmd) -> None:
    assert "pytest" in cmd.brief({"cmd": "pytest -x tests/"})
    assert "…" in cmd.brief({"cmd": "x" * 100})


def test_os_error_captured(cmd) -> None:
    """不存在的可执行文件：shell=True 时系统返回 exit_code 127/1 而非 OSError；
    直接 OSError 路径通过不存在的程序名触发。"""
    # shell=True 时不存在的命令走 shell 的 exit_code 而非 OSError，
    # 但某些平台仍可能报 FileNotFoundError；这里只验证不炸
    out = cmd.run("this_command_definitely_does_not_exist_xyz", timeout=5)
    assert "exit_code" in out


# ---- 命令守卫集成（M3）----


def test_blocked_command_raises_tool_error(cmd) -> None:
    with pytest.raises(Exception, match="拦截"):
        cmd.run("rm -rf /tmp/x", timeout=5)


def test_blocked_command_refused_even_after_approval(cmd) -> None:
    """黑名单是红线：approve 也无法解锁。"""
    cmd._guard.approve("rm -rf /tmp/x")
    with pytest.raises(Exception, match="拦截"):
        cmd.run("rm -rf /tmp/x", timeout=5)


def test_first_run_needs_confirm(cmd) -> None:
    assert cmd.needs_confirm({"cmd": "python -m pytest"}) is True


def test_blocked_command_does_not_need_confirm(cmd) -> None:
    """黑名单直接由 run 拒绝，不弹确认。"""
    assert cmd.needs_confirm({"cmd": "del /s C:\\x"}) is False


def test_approved_command_skips_confirm(cmd) -> None:
    assert cmd.needs_confirm({"cmd": "echo hi"}) is True
    cmd.run("echo hi", timeout=5)
    assert cmd.needs_confirm({"cmd": "echo hi"}) is False


def test_command_variant_requires_confirm_again(cmd) -> None:
    cmd.run("echo hi", timeout=5)
    assert cmd.needs_confirm({"cmd": "echo hello"}) is True


def test_confirm_title_and_preview(cmd) -> None:
    assert "pytest" in cmd.confirm_title({"cmd": "python -m pytest"})
    assert cmd.preview({"cmd": "pytest -x"}) == "$ pytest -x"
