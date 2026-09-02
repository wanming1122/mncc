"""bench 跑分 runner（M5）：20 任务评测管线，唯一事实源 M5_DESIGN.md。

real 模式（D3/D4）：每任务独立临时目录 → 拷贝 init/ + task.md → 写入项目级
.mncc.toml（按难度收紧 max_turns）→ 子进程 `mncc -p --yolo --stats-json`
→ 结束后拷入 test.py → 子进程 pytest 客观判分（D1/D2）。
mock 模式（D5）：per-task cassette.json 回放为脚本化客户端事件序列，进程内
驱动完整 main() 管线，零真实 API；结果标记 mode=mock、不进跑分统计（D6）。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest.mock as mock
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:  # `python bench/runner.py` 直接运行时可导入 mncc
    sys.path.insert(0, str(PROJECT_ROOT))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from mncc.config import ConfigError, parse_toml_subset  # noqa: E402
from mncc.llm.client import (  # noqa: E402
    CompletionResult,
    Event,
    LLMClient,
    LLMError,
    ResponseCompleted,
    TextDelta,
    ToolCall,
    Usage,
)

BENCH_DIR = PROJECT_ROOT / "bench"
TASKS_DIR = BENCH_DIR / "tasks"
RESULTS_DIR = BENCH_DIR / "results"

DIFFICULTY_MAX_TURNS = {"easy": 15, "medium": 20, "hard": 25}  # D3：难度分开管轮数上限
AGENT_TIMEOUT = 600  # 单任务 agent 硬超时（秒）
PYTEST_TIMEOUT = 120  # 评分 pytest 超时（§3.2）
CATEGORIES = ("bugfix", "feature", "refactor", "testwrite")
DIFFICULTIES = ("easy", "medium", "hard")


@dataclass(frozen=True)
class TaskMeta:
    task_id: str  # 目录名，全局唯一
    name: str
    category: str  # bugfix | feature | refactor | testwrite
    difficulty: str  # easy | medium | hard


@dataclass
class TaskResult:
    task_id: str
    mode: str  # real | mock
    status: str  # pass | fail | timeout | error | skipped
    exit_code: int | None = None  # mncc -p 退出码（D1：只记录不判分）
    verified: bool = False  # 终态 pytest 全绿（D1：判分依据）
    pytest_detail: str = ""  # 评分 pytest 的摘要/末行
    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    elapsed: float = 0.0
    error: str = ""  # timeout/error 时的人类可读说明


# ---- 任务元数据与发现 ----


def load_meta(task_id: str) -> TaskMeta:
    """读 tasks/<id>/meta.toml（复用 mncc 的 TOML 子集解析器，零新依赖）。"""
    task_dir = TASKS_DIR / task_id
    meta_path = task_dir / "meta.toml"
    if not meta_path.is_file():
        raise ConfigError(f"任务 {task_id} 缺少 meta.toml：{meta_path}")
    raw = parse_toml_subset(meta_path.read_text(encoding="utf-8"), source=str(meta_path))
    missing = {"name", "category", "difficulty"} - set(raw)
    if missing:
        raise ConfigError(f"{meta_path} 缺少字段：{', '.join(sorted(missing))}")
    name, category, difficulty = raw["name"], raw["category"], raw["difficulty"]
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{meta_path} 的 name 必须是字符串且非空")
    if category not in CATEGORIES:
        raise ConfigError(f"{meta_path} 非法 category={category!r}，可选：{CATEGORIES}")
    if difficulty not in DIFFICULTIES:
        raise ConfigError(f"{meta_path} 非法 difficulty={difficulty!r}，可选：{DIFFICULTIES}")
    return TaskMeta(task_id=task_id, name=name, category=category, difficulty=difficulty)


def discover_tasks(only: list[str] | None = None) -> list[str]:
    """发现全部任务 id（目录序稳定）；only 非空时校验存在性并只跑这些。"""
    ids = sorted(
        p.name for p in TASKS_DIR.iterdir() if p.is_dir() and (p / "meta.toml").is_file()
    )
    if only:
        unknown = [i for i in only if i not in ids]
        if unknown:
            have = ids or ["（无）"]
            raise ConfigError(f"未找到任务：{', '.join(unknown)}；已有任务：{', '.join(have)}")
        return only
    return ids


# ---- 工作目录准备与评分 ----


def prepare_workdir(task_dir: Path, workdir: Path) -> None:
    """D2/D3：拷贝 init/（递归）与 task.md；test.py 刻意不拷——评分才解锁。"""
    init_dir = task_dir / "init"
    if init_dir.is_dir():
        for item in init_dir.iterdir():
            dest = workdir / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
    shutil.copy2(task_dir / "task.md", workdir / "task.md")


def write_task_config(workdir: Path, meta: TaskMeta) -> None:
    """D3：注入项目级 .mncc.toml，按难度收紧 max_turns；其余配置沿用全局合并链。"""
    (workdir / ".mncc.toml").write_text(
        "# bench 注入（D3）：按难度收紧 max_turns，其余沿用户全局配置\n"
        f"max_turns = {DIFFICULTY_MAX_TURNS[meta.difficulty]}\n",
        encoding="utf-8",
    )


def _sub_env() -> dict[str, str]:
    # 让 `python -m mncc` / pytest 在任意 cwd（临时目录）下都能找到 mncc 包（verify_m3 模式）
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUTF8"] = "1"  # Windows 子进程统一 UTF-8（§3）
    return env


def run_scoring_pytest(workdir: Path) -> tuple[bool, str]:
    """D1 判分器：对终态工作目录跑 `pytest -q test.py`，返回码 0 即 verified。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "test.py"],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PYTEST_TIMEOUT,
            env=_sub_env(),
        )
    except subprocess.TimeoutExpired:
        return False, f"评分 pytest 超过 {PYTEST_TIMEOUT}s"
    lines = [ln for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip()]
    detail = lines[-1] if lines else f"退出码 {proc.returncode}"
    return proc.returncode == 0, detail


def _read_agent_stats(stats_path: Path) -> dict[str, Any]:
    """读 --stats-json 落盘（D4）。graceful 终态（含失败）都会落盘；
    只有外部强杀（timeout）没有文件——读不到就记 0，由 status=timeout 区分。

    只回轮数/token 四键：elapsed 由调用方用实测钟表（stats 的 elapsed 是
    进程内口径，与含子进程启停的墙钟不一致）。
    """
    if not stats_path.is_file():
        return {"turns": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    data = json.loads(stats_path.read_text(encoding="utf-8"))
    return {
        "turns": int(data.get("turns", 0)),
        "prompt_tokens": int(data.get("prompt_tokens", 0)),
        "completion_tokens": int(data.get("completion_tokens", 0)),
        "total_tokens": int(data.get("total_tokens", 0)),
    }


def _make_result(meta: TaskMeta, *, mode: str, status: str, **fields: Any) -> TaskResult:
    return TaskResult(task_id=meta.task_id, mode=mode, status=status, **fields)


# ---- mock 模式：cassette 回放（D5） ----


def _event_from_dict(raw: dict[str, Any]) -> Event:
    kind = raw.get("type")
    if kind == "TextDelta":
        return TextDelta(raw["text"])
    if kind == "ResponseCompleted":
        usage = raw.get("usage")
        tool_calls = [
            ToolCall(tc["id"], tc["name"], tc["arguments"])
            for tc in raw.get("tool_calls", [])
        ]
        return ResponseCompleted(
            content=raw.get("content", ""),
            usage=(
                Usage(usage["prompt"], usage["completion"], usage["total"]) if usage else None
            ),
            tool_calls=tool_calls,
        )
    raise ValueError(f"cassette 未知事件类型：{kind!r}")


class CassetteClient(LLMClient):
    """按轮次回放 cassette 事件序列（与 tests/test_loop.py 的 ScriptedClient 同构）。"""

    model = "cassette"

    def __init__(self, rounds: list[list[dict[str, Any]]]) -> None:
        self._rounds = rounds
        self._index = 0

    def stream(self, messages: list[dict[str, Any]], tools: Any = None) -> Iterator[Event]:
        if self._index >= len(self._rounds):
            # 转成 LLMError：循环按 error 终态收尾（退出码 1），而不是炸穿 main()
            raise LLMError(f"cassette 轮数不够：第 {self._index + 1} 轮模型调用无脚本")
        events = [_event_from_dict(raw) for raw in self._rounds[self._index]]
        self._index += 1
        yield from events

    def complete(self, messages: list[dict[str, Any]], max_tokens: Any = None) -> CompletionResult:
        return CompletionResult("cassette 摘要")  # 冒烟上下文小，不会触发压缩


def _run_mock(meta: TaskMeta, task_dir: Path, workdir: Path) -> TaskResult:
    """进程内跑完 `任务文案 → 参数解析 → 循环 → 落盘` 整条管线（无真实 API）。"""
    cassette_path = task_dir / "cassette.json"
    if not cassette_path.is_file():
        return _make_result(meta, mode="mock", status="skipped", error="无 cassette，mock 跳过")
    rounds = json.loads(cassette_path.read_text(encoding="utf-8"))
    task_text = (task_dir / "task.md").read_text(encoding="utf-8")
    stats_path = workdir / "stats.json"

    from mncc import cli

    start = time.monotonic()
    exit_code: int | None = None
    error = ""
    old_cwd = os.getcwd()
    try:
        os.chdir(workdir)
        # 同 fake_deps 手法：换掉唯一会碰网络的客户端；key 解析对冒烟无意义
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch.object(cli, "OpenAICompatClient", lambda **kwargs: CassetteClient(rounds)),
            mock.patch.object(cli, "resolve_api_key", lambda cfg: "sk-test"),
        ):
            exit_code = cli.main(["-p", task_text, "--yolo", "--stats-json", str(stats_path)])
    except Exception as exc:  # 管线自身崩溃也落一个可读结果，冒烟要能指出断点
        error = f"mock 管线异常：{type(exc).__name__}: {exc}"
    finally:
        os.chdir(old_cwd)
    elapsed = round(time.monotonic() - start, 2)

    if exit_code != 0:
        return _make_result(meta, mode="mock", status="error", exit_code=exit_code,
                            elapsed=elapsed, error=error or f"mncc -p 退出码 {exit_code}")
    shutil.copy2(task_dir / "test.py", workdir / "test.py")  # D2：评分才解锁
    verified, pytest_detail = run_scoring_pytest(workdir)
    stats = _read_agent_stats(stats_path)
    return _make_result(
        meta, mode="mock", status="pass" if verified else "fail",
        exit_code=exit_code, verified=verified, pytest_detail=pytest_detail,
        elapsed=elapsed, **stats,
    )


# ---- real 模式：子进程隔离（D3） ----


def _run_real(
    meta: TaskMeta, task_dir: Path, workdir: Path, *, model: str | None, timeout: int
) -> TaskResult:
    task_text = (task_dir / "task.md").read_text(encoding="utf-8")
    stats_path = workdir / "stats.json"
    cmd = [
        sys.executable, "-m", "mncc", "-p", task_text, "--yolo",
        "--stats-json", str(stats_path),
    ]
    if model:
        cmd += ["--model", model]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_sub_env(),
        )
    except subprocess.TimeoutExpired:
        return _make_result(meta, mode="real", status="timeout",
                            error=f"agent 超过 {timeout}s 未结束，已强杀（token 无落盘）")
    except OSError as exc:
        return _make_result(meta, mode="real", status="error", error=f"子进程启动失败：{exc}")
    elapsed = round(time.monotonic() - start, 2)
    exit_code = proc.returncode

    # D2：agent 全程看不到 test.py，跑完才拷入判分（防"照抄测试当实现"）
    shutil.copy2(task_dir / "test.py", workdir / "test.py")
    verified, pytest_detail = run_scoring_pytest(workdir)
    stats = _read_agent_stats(stats_path)
    error = ""
    if exit_code != 0:
        # 取 stderr 尾部多行（错误面板最后一行常是 rich 边框，无信息量）
        tail_lines = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()]
        tail = " | ".join(tail_lines[-6:]) or "无输出"
        error = f"mncc 退出码 {exit_code}：{tail}"
    return _make_result(
        meta, mode="real", status="pass" if verified else "fail",
        exit_code=exit_code, verified=verified, pytest_detail=pytest_detail,
        error=error, elapsed=elapsed, **stats,
    )


# ---- 任务执行与汇总 ----


def run_task(
    task: TaskMeta,
    *,
    mode: str,
    model: str | None = None,
    timeout: int = AGENT_TIMEOUT,
) -> TaskResult:
    """单任务独立临时目录：real 走子进程、mock 走进程内 cassette 回放。"""
    task_dir = TASKS_DIR / task.task_id
    with tempfile.TemporaryDirectory(prefix=f"mncc-bench-{task.task_id}-") as td:
        workdir = Path(td)
        prepare_workdir(task_dir, workdir)
        write_task_config(workdir, task)
        if mode == "mock":
            result = _run_mock(task, task_dir, workdir)
        else:
            result = _run_real(task, task_dir, workdir, model=model, timeout=timeout)
        result.elapsed = round(result.elapsed, 2)
        return result


def aggregate(results: list[TaskResult], metas: dict[str, TaskMeta]) -> dict[str, Any]:
    """汇总 pass_rate：总体 + 分难度 + 分分类（仅统计 real 结果，D5）。"""

    def stats(rows: list[TaskResult]) -> dict[str, Any]:
        total = len(rows)
        passed = sum(1 for r in rows if r.status == "pass")
        return {
            "total": total,
            "pass": passed,
            "fail": total - passed,
            "pass_rate": round(passed / total, 3) if total else 0.0,
        }

    def by(key: str, values: tuple[str, ...]) -> dict[str, Any]:
        return {v: stats([r for r in results if getattr(metas[r.task_id], key) == v])
                for v in values}

    return {
        **stats(results),
        "by_difficulty": by("difficulty", DIFFICULTIES),
        "by_category": by("category", CATEGORIES),
        "total_tokens": sum(r.total_tokens for r in results),
        "elapsed": round(sum(r.elapsed for r in results), 2),
    }


def git_head() -> str:
    """D6：跑分与 prompt 版本的可追溯锚点；非 git 环境返回空串。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def print_results_table(results: list[TaskResult], metas: dict[str, TaskMeta]) -> None:
    console = Console()
    table = Table(title=f"bench 结果（{len(results)} 任务）")
    for column in ("任务", "难度", "类别", "结果", "verified", "轮数", "tokens", "耗时 s"):
        table.add_column(column)
    colors = {"pass": "green", "fail": "red", "timeout": "yellow",
              "error": "red", "skipped": "dim"}
    for r in results:
        meta = metas[r.task_id]
        table.add_row(
            r.task_id,
            meta.difficulty,
            meta.category,
            f"[{colors[r.status]}]{r.status}[/]",
            {True: "✓", False: "✗"}.get(r.verified, "—"),
            str(r.turns),
            f"{r.total_tokens:,}",
            f"{r.elapsed:.1f}",
        )
    console.print(table)


def _run_payload(
    mode: str, results: list[TaskResult], metas: dict[str, TaskMeta], *,
    label: str, model: str | None, timeout: int,
) -> dict[str, Any]:
    real = [r for r in results if r.mode == "real"]
    return {
        "run_id": f"{mode}-{time.strftime('%Y%m%d-%H%M%S')}",
        "mode": mode,
        "label": label,
        "git_head": git_head(),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model or "",
        "config": {
            "difficulty_max_turns": DIFFICULTY_MAX_TURNS,
            "agent_timeout": timeout,
            "pytest_timeout": PYTEST_TIMEOUT,
        },
        # 明细补上难度/类别：报告渲染（report.py）只依赖结果文件本身
        "tasks": [
            {**asdict(r), "difficulty": metas[r.task_id].difficulty,
             "category": metas[r.task_id].category}
            for r in results
        ],
        "summary": aggregate(real, metas) if real else {},  # mock 不进跑分统计（D5）
    }


def run(
    mode: str,
    *,
    tasks: list[str] | None = None,
    label: str = "",
    model: str | None = None,
    timeout: int = AGENT_TIMEOUT,
    results_dir: Path | None = None,  # 测试可注入：默认参数就地捕获会绕过 monkeypatch
) -> int:
    """逐任务跑 → 汇总 → 落盘 results/<run_id>.json → 表格打印。全部通过返回 0。"""
    ids = discover_tasks(tasks)
    metas = {i: load_meta(i) for i in ids}
    results = [run_task(metas[i], mode=mode, model=model, timeout=timeout) for i in ids]

    payload = _run_payload(mode, results, metas, label=label, model=model, timeout=timeout)
    out_dir = results_dir or RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{payload['run_id']}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print_results_table(results, metas)
    executed = [r for r in results if r.status != "skipped"]
    if payload["summary"]:
        summary = payload["summary"]
        print(
            f"汇总：pass_rate {summary['pass_rate'] * 100:.1f}% "
            f"（{summary['pass']}/{summary['total']}），总 tokens {summary['total_tokens']:,}"
        )
    else:
        print("本轮无 real 结果（mock 冒烟不进跑分统计）")
    print(f"结果已落盘：{out_path}")
    return 0 if executed and all(r.status == "pass" for r in executed) else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench/runner.py", description="mncc bench 20 任务评测（M5）"
    )
    parser.add_argument("--mode", choices=["real", "mock"], default="real",
                        help="real=真实 API 跑分；mock=cassette 回放冒烟（无 API，CI 可用）")
    parser.add_argument("--tasks", nargs="*", metavar="ID", default=None,
                        help="只跑指定任务 id（空格分隔）；缺省跑全部")
    parser.add_argument("--label", default="", help="本轮跑分标签（写入 results JSON）")
    parser.add_argument("--model", default=None, help="透传给 mncc -p --model")
    parser.add_argument("--timeout", type=int, default=AGENT_TIMEOUT,
                        help=f"单任务 agent 硬超时秒数（默认 {AGENT_TIMEOUT}）")
    return parser


def _force_utf8_stdio() -> None:
    # 与 mncc/cli.py 同款：Windows 默认 GBK 控制台打不了表格里的 ✓/✗（§3）
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_arg_parser().parse_args(argv)
    try:
        exit_code = run(
            args.mode, tasks=args.tasks, label=args.label, model=args.model,
            timeout=args.timeout,
        )
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
