"""文件读写工具（read_file / write_file / edit_file；路径守卫 M3 接入）。

read_file 的输出设计服务于"模型消费"：
- 带行号（cat -n 风格）：模型引用行号比引用文本片段更稳，也为 edit_file
  的 diff 定位打底；
- 默认 2000 行上限 + 分页提示：大文件不主动截断提示会让模型以为读全了，
  基于残缺内容改代码是高频事故；
- 单行 2000 字符上限：一行压缩过的 minified js 就能吃掉大量上下文。

edit_file 的未命中诊断（决策 4）：先查行尾空白差异（只提示不自动改——
静默修正会掩盖模型真实错误，评测数据失真），再回显最相近片段 + mini diff。
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from ..safety import PathGuard
from .base import Tool, ToolError

MAX_LINES = 2000  # 单次读取行数上限（§4.3）
MAX_LINE_CHARS = 2000  # 单行字符上限，超出部分截断并标注
PREVIEW_LINES = 30  # write_file 确认预览的行数上限
MAX_DIFF_LINES = 150  # edit 确认预览的 diff 行数上限


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "读取文件内容（带行号）。修改任何文件之前必须先读它；不要基于记忆或猜测修改。"
        "默认最多返回 2000 行，文件更长时会提示后续 offset，用 offset/limit 分页读取。"
    )
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对当前目录或绝对路径）"},
            "offset": {"type": "integer", "description": "起始行号，从 1 开始；默认从头读取"},
            "limit": {"type": "integer", "description": "本次最多读取的行数；默认 2000"},
        },
        "required": ["path"],
    }

    def __init__(self, guard: PathGuard) -> None:
        self._guard = guard

    def brief(self, args: dict[str, Any]) -> str:
        return f"读取 {args.get('path', '?')}"

    def run(self, path: str, offset: int = 1, limit: int | None = None) -> str:
        p = self._guard.resolve(path)  # 守卫在最前：越界先于存在性检查被拒
        if not p.exists():
            raise ToolError(
                f"文件不存在：{path}（相对路径基于当前目录解析；请确认路径后重试）"
            )
        if p.is_dir():
            raise ToolError(f"{path} 是目录而不是文件；请用 list_dir 查看目录内容")
        try:
            # errors="replace"：GBK 等非 UTF-8 文件不会炸，坏字节显示为替换符
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"无法读取文件 {path}：{exc}") from exc

        lines = text.splitlines()
        total = len(lines)
        try:
            start = int(offset) - 1
        except (TypeError, ValueError):
            raise ToolError(f"offset 必须是整数，收到 {offset!r}") from None
        if start < 0:
            raise ToolError(f"offset 从 1 开始，收到 {offset!r}")
        if total > 0 and start >= total:
            raise ToolError(f"offset={offset} 超出文件总行数 {total}；请从更小的行号开始")
        if limit is None:
            cap = MAX_LINES
        else:
            try:
                cap = max(1, min(int(limit), MAX_LINES))
            except (TypeError, ValueError):
                raise ToolError(f"limit 必须是整数，收到 {limit!r}") from None

        selected = lines[start : start + cap]
        end = start + len(selected)
        width = len(str(end))
        out: list[str] = []
        for lineno, line in enumerate(selected, start=start + 1):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + f"…[本行超过 {MAX_LINE_CHARS} 字符，已截断]"
            out.append(f"{lineno:>{width}}\t{line}")
        if not out:
            return "（空文件）"
        body = "\n".join(out)
        if end < total:
            body += (
                f"\n\n[文件共 {total} 行，当前显示第 {start + 1}-{end} 行；"
                f"继续读取请传 offset={end + 1}]"
            )
        return body


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "把完整内容写入文件（新建或整体覆盖），父目录不存在会自动创建。"
        "写入前会向用户展示预览并请求确认。修改已有文件前应先 read_file 查看当前内容。"
    )
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件路径"},
            "content": {"type": "string", "description": "要写入的完整文件内容"},
        },
        "required": ["path", "content"],
    }

    def __init__(self, guard: PathGuard) -> None:
        self._guard = guard

    def brief(self, args: dict[str, Any]) -> str:
        return f"写入 {args.get('path', '?')}"

    def needs_confirm(self, args: dict[str, Any]) -> bool:
        return True

    def confirm_title(self, args: dict[str, Any]) -> str:
        return f"写入 {args.get('path', '?')}"

    def preview(self, args: dict[str, Any]) -> str:
        content = str(args.get("content", ""))
        lines = content.splitlines()
        head = "\n".join(lines[:PREVIEW_LINES])
        note = (
            ""
            if len(lines) <= PREVIEW_LINES
            else f"\n…（共 {len(lines)} 行，仅预览前 {PREVIEW_LINES} 行）"
        )
        return f"{args.get('path', '?')}\n{head or '（空内容）'}{note}"

    def run(self, path: str, content: str) -> str:
        p = self._guard.resolve(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # newline="\n"：固定 LF，跨平台产物一致，避免 Windows 下 \r\n 混入 diff
            p.write_text(content, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise ToolError(f"写入失败 {path}：{exc}") from exc
        n_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"已写入 {p}（{n_lines} 行，{len(content)} 字符）"


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "精确字符串替换：把文件中 old_string 替换为 new_string。"
        "old_string 必须与文件内容逐字符一致（含缩进与空白），且只出现一次；"
        "未命中或命中多处都会报错并给出提示。修改任何文件前必须先 read_file。"
        "修改前会展示 diff 并请求确认。"
    )
    preview_lexer = "diff"
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要修改的文件路径（必须已存在）"},
            "old_string": {"type": "string", "description": "文件中待替换的原文片段"},
            "new_string": {"type": "string", "description": "替换后的内容"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def __init__(self, guard: PathGuard) -> None:
        self._guard = guard

    def brief(self, args: dict[str, Any]) -> str:
        return f"编辑 {args.get('path', '?')}"

    def needs_confirm(self, args: dict[str, Any]) -> bool:
        return True

    def confirm_title(self, args: dict[str, Any]) -> str:
        return f"编辑 {args.get('path', '?')}"

    # ---- 内部：读文件 + 行尾归一 ----

    def _read_normalized(self, path: str) -> tuple[Path, str, str]:
        """读文件并在内存中把行尾统一为 LF。返回 (真实路径, 归一文本, 主导行尾)。

        行尾保留（决策 3）：读 bytes 检测主导行尾，匹配/替换在 LF 世界进行，
        写回时还原——否则 Windows CRLF 文件一编辑就全文件变行尾，git diff 全红。
        """
        resolved = self._guard.resolve(path)
        if not resolved.exists():
            raise ToolError(
                f"文件不存在：{path}。edit_file 只能修改已存在的文件；"
                "新建文件请用 write_file"
            )
        if resolved.is_dir():
            raise ToolError(f"{path} 是目录而不是文件；edit_file 只能修改文件")
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            raise ToolError(f"无法读取文件 {path}：{exc}") from exc
        if b"\0" in raw[:8192]:
            raise ToolError(f"{path} 看起来是二进制文件，edit_file 只能修改文本文件")
        crlf = raw.count(b"\r\n")
        lf = raw.count(b"\n") - crlf
        dominant = "\r\n" if crlf > lf else "\n"
        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
        return resolved, text, dominant

    @staticmethod
    def _line_no(text: str, pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    def _locate_exactly_once(self, text: str, old_string: str, path: str) -> int:
        """定位 old_string 的唯一次出现，返回起始位置；0 次/多次给诊断并抛错。"""
        count = text.count(old_string)
        if count == 1:
            return text.find(old_string)
        if count == 0:
            raise ToolError(self._diagnose_miss(text, old_string, path))
        # 多处命中：列出所有行号，让模型扩大上下文
        rows: list[str] = []
        idx = text.find(old_string)
        while idx != -1:
            rows.append(str(self._line_no(text, idx)))
            idx = text.find(old_string, idx + 1)
        raise ToolError(
            f"old_string 在文件中命中 {count} 处（第 {', '.join(rows)} 行），不唯一。"
            f"请扩大 old_string 的上下文（前后多带几行）使其唯一后重试"
        )

    def _diagnose_miss(self, text: str, old_string: str, path: str) -> str:
        """0 次命中的诊断（决策 4）：先查行尾空白差异，再回显最相近片段。"""
        # 空白差异：逐行 rstrip 归一后唯一命中 → 只提示，不自动改（自动改会
        # 掩盖模型的真实错误，评测数据失真）
        normalized = "\n".join(line.rstrip() for line in old_string.split("\n"))
        if normalized != old_string and text.count(normalized) == 1:
            pos = text.find(normalized)
            return (
                f"old_string 未命中，但去掉每行行尾空白后能在第 {self._line_no(text, pos)} 行"
                f"唯一命中——疑似行尾空格/制表符差异。请 read_file 查看原文后逐字符重发"
            )
        # 最相近片段：行级最长公共块，回显上下文 + 与 old_string 的 mini diff
        matcher = difflib.SequenceMatcher(None, text, old_string)
        match = matcher.find_longest_match(0, len(text), 0, len(old_string))
        line_no = self._line_no(text, match.a)
        lines = text.split("\n")
        start = max(0, line_no - 3)
        end = min(len(lines), line_no + 2)
        width = len(str(end + 1))
        actual = "\n".join(
            f"{n:>{width}}\t{lines[n - 1]}" for n in range(start + 1, end + 1)
        )
        mini = "\n".join(
            difflib.unified_diff(
                lines[start:end], old_string.split("\n"), lineterm="", n=1
            )
        ) or "（对比片段为空）"
        return (
            f"old_string 未在 {path} 中找到（0 次命中）。"
            f"最相近位置在第 {line_no} 行附近：\n--- 文件中该处实际内容 ---\n{actual}\n"
            f"--- 与 old_string 的差异 ---\n{mini}\n"
            f"请 read_file 确认精确内容后重试"
        )

    # ---- preview / run ----

    def preview(self, args: dict[str, Any]) -> str:
        """修改前的 unified diff（rich 以 diff 词法高亮）。命中校验失败照实抛错，
        由 registry 转为 is_error 回填——用户不会为注定失败的修改被询问。"""
        path = str(args["path"])
        resolved, text, dominant = self._read_normalized(path)
        old_string = str(args.get("old_string", ""))
        new_string = str(args.get("new_string", ""))
        pos = self._locate_exactly_once(text, old_string, path)
        new_text = text[:pos] + new_string + text[pos + len(old_string) :]
        diff = difflib.unified_diff(
            text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"{path}（修改前）",
            tofile=f"{path}（修改后）",
        )
        lines = "".join(diff).splitlines()
        head = "\n".join(lines[:MAX_DIFF_LINES])
        note = "" if len(lines) <= MAX_DIFF_LINES else "\n…（diff 过长，仅展示前段）"
        eol_label = "CRLF" if dominant == "\r\n" else "LF"
        return f"{head}{note}\n（行尾：{eol_label}）"

    def run(self, path: str, old_string: str, new_string: str) -> str:
        resolved, text, dominant = self._read_normalized(path)
        if old_string == new_string:
            raise ToolError("old_string 与 new_string 相同，没有可应用的修改")
        pos = self._locate_exactly_once(text, old_string, path)
        new_text = text[:pos] + new_string + text[pos + len(old_string) :]
        try:
            # 按原文件主导行尾写回（write_bytes 绕开文本模式的行尾转换）
            resolved.write_bytes(new_text.replace("\n", dominant).encode("utf-8"))
        except OSError as exc:
            raise ToolError(f"写入失败 {path}：{exc}") from exc
        line_no = self._line_no(text, pos)
        removed = old_string.count("\n") + 1
        added = new_string.count("\n") + 1
        return f"已修改 {path}：第 {line_no} 行附近，替换 1 处（-{removed} 行 / +{added} 行）"
