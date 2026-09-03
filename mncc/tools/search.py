"""探索类工具（M3）：list_dir 树形列表 + grep 正则搜索。

两个都是"只读探测"：输出量受硬上限约束，是模型在动手前建立事实的入口
（system prompt 纪律 2"不猜文件"的配套工具）。

限额的切入点：
- list_dir 限 500 项、深度 8——目录树会指数膨胀，无上限的一次调用就能
  吃掉整个上下文预算（这也正是 §5.B 子代理要解决的"搜索污染主上下文"问题，
  M6 选做时以本模块为基础加只读白名单即可）。
- grep 限 100 条——匹配太多说明搜索条件不够具体，回传前 100 条 + 提示
  缩小范围，比灌给模型上万行匹配结果更有用。
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from ..safety import PathGuard
from .base import Tool, ToolError

# 树形/递归遍历统一忽略的目录与文件模式（§4.3）
DEFAULT_IGNORES = {
    ".git",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    ".mncc",
    "dist",
    "build",
    "*.egg-info",
}

MAX_ENTRIES = 500  # list_dir 单项数上限
MAX_TREE_DEPTH = 8  # list_dir 递归深度上限
MAX_GREP_MATCHES = 100  # grep 匹配数上限（§4.3）
MAX_GREP_LINE_CHARS = 200  # grep 单行回显截断
MAX_FILE_SIZE = 4 * 1024 * 1024  # grep 跳过超过此大小的文件
BINARY_PROBE_BYTES = 8192  # 判二进制的取样窗口


def _is_ignored(name: str) -> bool:
    return name in DEFAULT_IGNORES or any(
        fnmatch.fnmatch(name, pat) for pat in DEFAULT_IGNORES if any(c in pat for c in "*?[")
    )


class ListDirTool(Tool):
    name = "list_dir"
    description = (
        "以树形结构列出目录内容（目录在前、文件在后，均按名称排序），"
        f"输出路径相对工作区根；自动忽略 {', '.join(sorted(DEFAULT_IGNORES))} 等。"
        "用于了解项目结构、定位文件位置；动手改代码前先用它确认文件在哪。"
    )
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要列出的目录路径，默认当前目录"},
        },
        "required": [],
    }

    def __init__(self, guard: PathGuard) -> None:
        self._guard = guard

    def brief(self, args: dict[str, Any]) -> str:
        return f"列出 {args.get('path', '.')}"

    def run(self, path: str = ".") -> str:
        base = self._guard.resolve(path)
        if not base.exists():
            raise ToolError(f"目录不存在：{path}")
        if not base.is_dir():
            raise ToolError(f"{path} 不是目录；请用 read_file 读取文件内容")

        budget = MAX_ENTRIES
        root_label = "."
        try:
            root_label = base.relative_to(self._guard.root).as_posix() or "."
        except ValueError:  # base == root 时 relative_to 返回 "."，正常不会走到这
            root_label = str(base)

        def render(dirpath: Path, prefix: str, depth: int) -> list[str]:
            nonlocal budget
            lines: list[str] = []
            if depth > MAX_TREE_DEPTH:
                lines.append(prefix + "…（已达最大深度）")
                return lines
            try:
                entries = sorted(
                    os.scandir(dirpath),
                    key=lambda e: (not e.is_dir(), e.name.casefold()),
                )
            except OSError as exc:
                lines.append(prefix + f"（无法读取：{exc}）")
                return lines
            for i, entry in enumerate(entries):
                if _is_ignored(entry.name):
                    continue
                if budget <= 0:
                    lines.append(prefix + "…（条目过多，已截断）")
                    budget -= 1
                    return lines
                budget -= 1
                is_last = i == len(entries) - 1
                branch = "└── " if is_last else "├── "
                child_prefix = prefix + ("    " if is_last else "│   ")
                is_dir = entry.is_dir()
                name = entry.name + ("/" if is_dir else "")
                lines.append(prefix + branch + name)
                # 符号链接目录不递归进入：链接可指向工作区外（信息泄漏）或成环
                if is_dir and not entry.is_symlink():
                    lines.extend(render(Path(entry.path), child_prefix, depth + 1))
            return lines

        body = "\n".join(render(base, "", 1))
        return f"{root_label}/\n{body}" if body else f"{root_label}/（空目录）"


class GrepTool(Tool):
    name = "grep"
    description = (
        "用正则表达式搜索文件内容，输出格式：相对路径:行号:内容。"
        "用于定位函数/类定义、报错来源与调用点。"
        f"默认最多返回 {MAX_GREP_MATCHES} 条匹配；匹配过多时应加 path 限定目录、"
        "glob 限定文件类型（如 *.py）或收紧正则。二进制文件自动跳过。"
    )
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式（Python re 语法）"},
            "path": {"type": "string", "description": "搜索目录（相对当前目录），默认当前目录"},
            "glob": {"type": "string", "description": "文件名过滤，如 *.py；默认不过滤"},
        },
        "required": ["pattern"],
    }

    def __init__(self, guard: PathGuard) -> None:
        self._guard = guard

    def brief(self, args: dict[str, Any]) -> str:
        scope = args.get("path", ".") if args.get("path") else "."
        return f"grep {args.get('pattern', '?')[:40]} in {scope}"

    def _iter_files(self, base: Path, glob: str | None):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not _is_ignored(d)]
            for fname in sorted(filenames):
                if _is_ignored(fname):
                    continue
                if glob and not fnmatch.fnmatch(fname, glob):
                    continue
                yield Path(dirpath) / fname

    def run(self, pattern: str, path: str = ".", glob: str | None = None) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"正则表达式不合法：{pattern}（{exc}）；请修正后重试") from None

        base = self._guard.resolve(path)
        if not base.exists():
            raise ToolError(f"搜索路径不存在：{path}")
        if not base.is_dir():
            raise ToolError(f"{path} 不是目录；grep 按目录递归搜索，单文件请用 read_file")

        out: list[str] = []
        truncated = False
        for f in self._iter_files(base, glob):
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_SIZE:
                continue
            try:
                raw = f.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:BINARY_PROBE_BYTES]:
                continue  # 二进制文件：正则匹配无意义，跳过
            text = raw.decode("utf-8", errors="replace")
            for ln, line in enumerate(text.splitlines(), start=1):
                if not rx.search(line):
                    continue
                content = line[:MAX_GREP_LINE_CHARS]
                if len(line) > MAX_GREP_LINE_CHARS:
                    content += "…"
                try:
                    # as_posix：跨平台统一为正斜杠（代码引用习惯），且与 read_file 定位衔接
                    rel = f.relative_to(self._guard.root).as_posix()
                except ValueError:
                    rel = f.as_posix()
                out.append(f"{rel}:{ln}:{content}")
                if len(out) >= MAX_GREP_MATCHES + 1:
                    truncated = True
                    break
            if truncated:
                break

        if truncated:
            out = out[:MAX_GREP_MATCHES]
        if not out:
            return (
                f"未找到匹配（pattern={pattern!r}，范围 {path}{f'，glob={glob}' if glob else ''}）"
            )
        body = "\n".join(out)
        if truncated:
            body += (
                f"\n…（已达 {MAX_GREP_MATCHES} 条上限，还有更多匹配；"
                "请缩小 path 范围、加 glob 过滤或收紧正则）"
            )
        return body
