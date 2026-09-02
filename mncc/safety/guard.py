"""安全守卫（§4.4）：路径守卫 + 命令守卫。

本模块零项目内依赖（不 import mncc 其他任何模块）——守卫是安全边界，
依赖越少越容易审计、越难被无意破坏。tools → safety 单向依赖。

- PathGuard：把文件操作限制在工作区内。resolve() 先展开 `../` 与符号链接，
  再做包含判断——字符串前缀在界内不等于解析结果在界内，穿越攻击正是靠
  这两者的差异。
- CommandGuard：黑名单绝对拦截（--yolo 不解锁，决策 1）；其余命令首次执行
  需确认，授权按精确命令串记忆（决策 2）——批准 python -m pytest 不会连带
  放行 python -c "os.remove(...)"。

黑名单是启发式正则，能拦住的只是"长成危险样子的命令"；shell 命令无法通过
静态分析可靠沙箱化（python -c 里能写任意路径），这是诚实声明过的边界——
真正的进程隔离超出本项目范围。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ---- 路径守卫 ----

WORKSPACE_ERROR_HINT = (
    "所有文件操作必须限制在工作区（启动目录）内；"
    "请确认路径拼写，如确实需要访问工作区外的文件，请向用户说明并请求人工操作"
)


class SafetyViolation(Exception):
    """守卫拒绝。message 面向模型：说明原因 + 正确做法。"""


class PathGuard:
    """把所有文件操作限制在工作区（启动目录）内。

    只做解析与判定、不碰文件系统其他部分；被所有文件/搜索工具共享，
    保证"每一处文件操作都过同一道闸门"。
    """

    def __init__(self, root: str | Path) -> None:
        # 构造时 resolve 固化：root 的符号链接在启动时一次展开，之后是稳定锚点
        self.root = Path(root).resolve()

    def resolve(self, path: str) -> Path:
        """把路径解析到工作区内的真实位置；越界 raise SafetyViolation。"""
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        try:
            resolved = p.resolve()
        except OSError as exc:  # 深度嵌套/权限异常等平台特例，解析不了就不放行
            raise SafetyViolation(f"路径无法解析：{path}（{exc}）。{WORKSPACE_ERROR_HINT}") from exc
        if resolved != self.root and self.root not in resolved.parents:
            raise SafetyViolation(
                f"路径越界：{path} 解析后（{resolved}）不在工作区 {self.root} 内。"
                f"{WORKSPACE_ERROR_HINT}"
            )
        return resolved


# ---- 命令守卫 ----

@dataclass(frozen=True)
class CommandVerdict:
    """一次命令检查的结论。"""

    action: str  # "allow" | "confirm" | "block"
    reason: str = ""


# (正则, 拦截理由)。re.IGNORECASE 匹配（Windows 命令大小写不敏感）。
# 每条都是"长成危险样子的命令"的最小特征；宁可错杀一条本想删自己文件的命令，
# 也不能放行一条删整个盘的命令——模型被拦后会遇到 is_error 并换方案。
_BLOCKED_PATTERNS: list[tuple[str, str]] = [
    # rm 带 r/f 旗标（-rf、-fr、--recursive --force 及其任意组合）
    (r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*|--recursive|--force)(\s|$)", "rm 的递归/强制删除"),
    # Windows 递归删除
    (r"\b(?:del|erase)\b(?=[^|&]*/[a-zA-Z]*s\b)", "del 的 /s 递归删除"),
    (r"\brmdir\b(?=[^|&]*/[a-zA-Z]*s\b)", "rmdir 的 /s 递归删除"),
    # 格式化磁盘
    (r"\bmkfs(?:\.\S+)?\b", "格式化文件系统（mkfs）"),
    (r"\bformat\b\s+[a-z]\s*:", "格式化磁盘（format 盘符）"),
    # 管道到 shell 执行远程内容
    (r"\b(?:curl|wget)\b[^|&\n]*\|\s*(?:ba)?sh\b", "下载脚本并直接交给 shell 执行（curl|sh）"),
    # 直接写块设备
    (r"\bdd\b[^|&\n]*\bof\s*=\s*/dev/(?!null\b)", "dd 直接写块设备"),
    # fork 炸弹
    (r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;", "fork 炸弹"),
]

_BLOCK_HINT = (
    "请改用更安全的等效操作（如删除单个指定文件、在受控目录内操作），"
    "或向用户说明原因并请求人工处理"
)


class CommandGuard:
    """黑名单必拦截；其余命令首次执行需确认；授权按精确命令串会话级记忆。"""

    def __init__(self) -> None:
        self._approved: set[str] = set()

    def check(self, cmd: str) -> CommandVerdict:
        cmd = cmd.strip()
        if not cmd:
            return CommandVerdict("block", "空命令")
        # 黑名单先于已授权集合判断：approve 没有解锁黑名单的能力（决策 1），
        # 顺序反了就会出现"批准过的 rm -rf 反而放行"的漏洞
        for pattern, reason in _BLOCKED_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                return CommandVerdict("block", reason)
        if cmd in self._approved:
            return CommandVerdict("allow")
        return CommandVerdict("confirm")

    def approve(self, cmd: str) -> None:
        """记住已授权的精确命令串。不是按程序名首 token：授权粒度必须
        等于被授权对象，否则批准 python -m pytest 就顺带授权了任意 python 代码。"""
        self._approved.add(cmd.strip())
