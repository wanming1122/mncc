"""命令执行工具（run_command）。

M3 接入命令守卫：黑名单（rm -rf / mkfs / format / curl|sh / del /s 等）绝对
拦截——--yolo 只跳过"确认"不跳过"红线"（决策 1）；其余命令首次执行需
确认，授权按精确命令串记忆（决策 2）。超时强杀与输出截断自 M2 保留：
防止进程失控与上下文爆炸。
"""

from __future__ import annotations

import os
import subprocess
import threading
from typing import Any

from ..safety import CommandGuard
from .base import Tool, ToolError

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 600  # 模型可能传天文数字的 timeout；上限钳制防止任务假死
CLIP_LIMIT = 8000  # 单段输出上限
CLIP_TAIL = 2000  # 截断时保留的尾部字符数


def _clip(text: str, limit: int = CLIP_LIMIT, tail: int = CLIP_TAIL) -> str:
    """超长输出保留首尾，中间省略——首尾通常是命令横幅与最终报错，信息密度最高。"""
    if len(text) <= limit:
        return text
    head = limit - tail
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…[中间省略约 {omitted:,} 字符]…\n{text[-tail:]}"


def _as_text(data: object) -> str:
    """TimeoutExpired 携带的输出在平台间类型不一（None/bytes/str），统一转 str。"""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "在当前目录执行 shell 命令，返回 stdout / stderr / exit_code。"
        "用于运行测试、脚本、构建与版本管理等操作，是验证修改结果的唯一手段。"
        "输出过长会截断；超过 timeout 秒进程被强制终止。"
    )
    schema = {
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "要执行的命令字符串"},
            "timeout": {
                "type": "integer",
                "description": f"超时秒数（1-{MAX_TIMEOUT}），默认 {DEFAULT_TIMEOUT}",
            },
        },
        "required": ["cmd"],
    }

    def __init__(self, guard: CommandGuard) -> None:
        self._guard = guard

    def brief(self, args: dict[str, Any]) -> str:
        cmd = str(args.get("cmd", "?"))
        return f"执行 {cmd[:60]}{'…' if len(cmd) > 60 else ''}"

    def needs_confirm(self, args: dict[str, Any]) -> bool:
        # 黑名单返回 block → needs_confirm=False（不弹确认，直接由 run 拒绝）；
        # 已授权的精确命令串返回 allow → 免确认
        return self._guard.check(str(args.get("cmd", ""))).action == "confirm"

    def confirm_title(self, args: dict[str, Any]) -> str:
        cmd = str(args.get("cmd", "?"))
        return f"执行命令：{cmd[:60]}{'…' if len(cmd) > 60 else ''}"

    def preview(self, args: dict[str, Any]) -> str:
        return f"$ {args.get('cmd', '')}"

    def run(self, cmd: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        verdict = self._guard.check(cmd)
        if verdict.action == "block":
            # 黑名单检查在需要确认之前、且不受 yolo 影响（决策 1）
            raise ToolError(
                f"命令被安全策略拦截：{verdict.reason}。{cmd!r} 属于高危操作。"
                "请改用更安全的等效操作（如删除单个指定文件、在受控目录内操作），"
                "或向用户说明原因并请求人工处理"
            )
        self._guard.approve(cmd)  # 通过检查即记住：下一轮重跑同一条命令免确认
        try:
            t = max(1, min(int(timeout), MAX_TIMEOUT))
        except (TypeError, ValueError):
            raise ToolError(f"timeout 必须是 1-{MAX_TIMEOUT} 的整数，收到 {timeout!r}") from None

        # PYTHONUTF8=1 让子进程里的 Python 也输出 UTF-8；本进程侧再用 utf-8 解码，
        # 两端统一避免 Windows 默认 GBK 乱码（§3）
        env = {**os.environ, "PYTHONUTF8": "1"}
        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise ToolError(f"命令启动失败：{exc}") from exc

        # 读取线程持续泵取输出：这样超时强杀时已产生的部分输出不会丢
        # （communicate(timeout) 的 TimeoutExpired 在 Windows 上不带部分输出，
        # subprocess.run 在 POSIX 上干脆不收集——两者都不可靠）
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []

        def _pump(stream: object, sink: list[bytes]) -> None:
            try:
                while True:
                    chunk = stream.read1(4096)  # type: ignore[attr-defined]
                    if not chunk:
                        break
                    sink.append(chunk)
            except (OSError, ValueError):
                pass  # 进程被杀/流关闭：已读到的内容保留

        pump_threads = [
            threading.Thread(target=_pump, args=(proc.stdout, out_chunks), daemon=True),
            threading.Thread(target=_pump, args=(proc.stderr, err_chunks), daemon=True),
        ]
        for thread in pump_threads:
            thread.start()

        timed_out = False
        try:
            proc.wait(timeout=t)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                proc.kill()  # 杀掉 shell 后立即收尸，避免僵尸
                proc.wait()
            except OSError:
                pass
        for thread in pump_threads:
            # 超时后：子进程可能残留（Windows 孤儿），join 限时避免阻塞
            thread.join(timeout=2 if timed_out else None)

        stdout = _as_text(b"".join(out_chunks))
        stderr = _as_text(b"".join(err_chunks))
        if timed_out:
            partial = _clip(stdout + stderr)
            hint = f"\n已产生的输出：\n{partial}" if partial.strip() else ""
            raise ToolError(f"命令超过 {t} 秒未结束，已强制终止：{cmd}{hint}") from None

        returncode = proc.returncode

        sections = [f"exit_code: {returncode}"]
        if stdout:
            sections.append(f"--- stdout ---\n{_clip(stdout)}")
        if stderr:
            sections.append(f"--- stderr ---\n{_clip(stderr)}")
        if len(sections) == 1:
            sections.append("（无输出）")
        # 非零退出码不算 is_error：exit_code 本身就是要回传给模型的事实，
        # 由模型决定下一步（重试/换方案/汇报失败）
        return "\n".join(sections)
