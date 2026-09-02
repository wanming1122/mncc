"""bench 结果报告（M5/D6）：results/*.json → README 可粘贴的 markdown / 对比表。

单份跑分：总表 + 分难度/分分类 pass_rate；两份跑分：逐任务 老→新 对比 +
各维度增量——PROMPT §2 简历卖点"__% → __%"的数据管道必须是可复现工具。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402


def load_run(path: str | Path) -> dict[str, Any]:
    """读 results/*.json。文件不存在时报错并列出已有结果文件。"""
    p = Path(path)
    if not p.is_file():
        results_dir = p.parent if p.parent.is_dir() else PROJECT_ROOT / "bench" / "results"
        existing = ", ".join(sorted(x.name for x in results_dir.glob("*.json"))) or "（无）"
        raise FileNotFoundError(f"结果文件不存在：{p}；已有：{existing}")
    return json.loads(p.read_text(encoding="utf-8"))


def _pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def _header(run: dict[str, Any]) -> str:
    extra = f"，label={run['label']}" if run.get("label") else ""
    return f"run_id {run['run_id']}（mode={run['mode']}，git {run['git_head'] or '?'}{extra}）"


def _task_row(run: dict[str, Any], task: dict[str, Any], statuses: dict[str, str]) -> str:
    return (
        f"| {task['task_id']} | {task['difficulty']} | {task['category']} | "
        f"{statuses.get(task['status'], task['status'])} | {task['turns']} | "
        f"{task['total_tokens']} | {task['elapsed']:.1f}s |"
    )


def _dimension_rows(run: dict[str, Any]) -> list[str]:
    summary = run.get("summary") or {}
    rows: list[str] = []
    labels = [("by_difficulty", "难度"), ("by_category", "类别")]
    for key, title in labels:
        dim = summary.get(key) or {}
        if not dim:
            continue
        rows.append(f"| {title} | 通过/总数 | pass_rate |")
        rows.append("|---|---|---|")
        for name, stats in dim.items():
            rows.append(f"| {name} | {stats['pass']}/{stats['total']} | "
                        f"{_pct(stats['pass_rate'])} |")
    return rows


def markdown_table(run: dict[str, Any]) -> str:
    """README 可直接粘贴的单份跑分 markdown（含逐任务明细与维度分布）。"""
    statuses = {"pass": "通过", "fail": "失败", "timeout": "超时", "error": "错误"}
    rows = [
        f"### 跑分 {_header(run)}",
        "",
        "| 任务 | 难度 | 类别 | 结果 | 轮数 | tokens | 耗时 |",
        "|---|---|---|---|---|---|---|",
    ]
    rows += [_task_row(run, t, statuses) for t in run.get("tasks", [])]
    summary = run.get("summary") or {}
    if summary:
        rows += [
            "",
            f"**汇总**：通过 {summary['pass']}/{summary['total']}，"
            f"pass_rate **{_pct(summary['pass_rate'])}**，总 tokens {summary['total_tokens']:,}",
            "",
            *_dimension_rows(run),
        ]
    return "\n".join(rows)


def summary_table(run: dict[str, Any]) -> Table:
    """单份跑分的人类可读终端表格（rich）。"""
    table = Table(title=f"bench 跑分 {_header(run)}")
    for column in ("任务", "难度", "类别", "结果", "verified", "轮数", "tokens", "耗时 s"):
        table.add_column(column)
    colors = {"pass": "green", "fail": "red", "timeout": "yellow",
              "error": "red", "skipped": "dim"}
    for task in run.get("tasks", []):
        table.add_row(
            task["task_id"], task["difficulty"], task["category"],
            f"[{colors.get(task['status'], 'white')}]{task['status']}[/]",
            {True: "✓", False: "✗"}.get(task["verified"], "—"),
            str(task["turns"]), f"{task['total_tokens']:,}", f"{task['elapsed']:.1f}",
        )
    summary = run.get("summary") or {}
    if summary:
        table.caption = (
            f"pass_rate {_pct(summary['pass_rate'])}（{summary['pass']}/{summary['total']}）"
            f"，总 tokens {summary['total_tokens']:,}"
        )
    return table


def compare_table(old: dict[str, Any], new: dict[str, Any]) -> str:
    """两份跑分对比：逐任务 老→新 + 总分/分档/分类增量（D6）。"""
    statuses = {"pass": "通过", "fail": "失败", "timeout": "超时", "error": "错误"}
    old_tasks = {t["task_id"]: t for t in old.get("tasks", [])}
    rows = [
        f"### 对比 {_header(old)} → {_header(new)}",
        "",
        "| 任务 | 老结果 | 新结果 | 老轮数 | 新轮数 |",
        "|---|---|---|---|---|",
    ]
    for task in new.get("tasks", []):
        tid = task["task_id"]
        prev = old_tasks.get(tid)
        if prev is None:
            continue
        rows.append(
            f"| {tid} | {statuses.get(prev['status'], prev['status'])} | "
            f"{statuses.get(task['status'], task['status'])} | {prev['turns']} | "
            f"{task['turns']} |"
        )

    old_sum, new_sum = old.get("summary") or {}, new.get("summary") or {}
    if old_sum and new_sum:
        rows += [
            "",
            f"**总体**：{_pct(old_sum['pass_rate'])} → {_pct(new_sum['pass_rate'])}"
            f"（{old_sum['pass']} → {new_sum['pass']} / {new_sum['total']}）",
        ]
        for key, title in (("by_difficulty", "难度"), ("by_category", "类别")):
            old_dim, new_dim = old_sum.get(key) or {}, new_sum.get(key) or {}
            changes = []
            for name in new_dim:
                if name not in old_dim:
                    continue
                o, n = old_dim[name], new_dim[name]
                changes.append(f"{name} {_pct(o['pass_rate'])} → {_pct(n['pass_rate'])}")
            if changes:
                rows.append(f"- {title}：{'; '.join(changes)}")
    return "\n".join(rows)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench/report.py",
        description="bench 结果报告：单份跑分表 / 两份对比表（--md 输出 README 可粘贴 markdown）",
    )
    parser.add_argument("runs", nargs="+", metavar="results/*.json", help="一份或两份结果文件")
    parser.add_argument("--md", action="store_true", help="只输出纯 markdown（不带 rich 表格）")
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
        runs = [load_run(p) for p in args.runs]
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"读取结果失败：{exc}", file=sys.stderr)
        return 1
    if len(args.runs) == 1:
        if args.md:
            print(markdown_table(runs[0]))
        else:
            Console().print(summary_table(runs[0]))
        return 0
    if len(args.runs) == 2:
        print(compare_table(runs[0], runs[1]))
        return 0
    print("只支持一份（跑分表）或两份（对比表）结果文件", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
