"""bench 管线（M5）：任务发现、meta 解析、init 拷贝 + 配置注入、评分器、mock 端到端、
汇总聚合与报告输出。全部无真实 API（协议：bench/ 下 20 任务本身不是单测对象）。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bench import report, runner
from bench.runner import (
    DIFFICULTY_MAX_TURNS,
    TASKS_DIR,
    TaskMeta,
    TaskResult,
    aggregate,
    discover_tasks,
    load_meta,
    prepare_workdir,
    run,
    run_scoring_pytest,
    run_task,
    write_task_config,
)
from mncc.config import ConfigError

SMOKE_TASKS = ["bf_calc_ops", "ft_csv_stats", "tw_str_utils"]


# ---- meta 解析与任务发现 ----


def test_load_meta_parses_fields() -> None:
    meta = load_meta("bf_calc_ops")
    assert meta == TaskMeta(
        task_id="bf_calc_ops", name="计算器两个 bug", category="bugfix", difficulty="medium"
    )


def test_load_meta_missing_file_raises(tmp_path: Path) -> None:
    (tmp_path / "ghost").mkdir()
    with pytest.raises(ConfigError):
        _load_meta_from(tmp_path / "ghost")


def test_load_meta_invalid_difficulty_raises(tmp_path: Path) -> None:
    d = tmp_path / "bad"
    d.mkdir()
    (d / "meta.toml").write_text(
        'name = "x"\ncategory = "bugfix"\ndifficulty = "nightmare"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="difficulty"):
        _load_meta_from(d)


def _load_meta_from(task_dir: Path) -> TaskMeta:
    """load_meta 以 TASKS_DIR 为根，这里用 monkeypatch 换根测边界分支。"""
    from unittest import mock

    with mock.patch.object(runner, "TASKS_DIR", task_dir.parent):
        return load_meta(task_dir.name)


def test_discover_tasks_finds_twenty() -> None:
    ids = discover_tasks()
    assert len(ids) == 20
    # 分布抽查：类别与难度配额（D7）
    metas = [load_meta(i) for i in ids]
    assert all(m.task_id in ids for m in metas)
    assert sum(1 for m in metas if m.category == "bugfix") == 6
    assert sum(1 for m in metas if m.category == "feature") == 6
    assert sum(1 for m in metas if m.category == "refactor") == 4
    assert sum(1 for m in metas if m.category == "testwrite") == 4
    assert sum(1 for m in metas if m.difficulty == "easy") == 6
    assert sum(1 for m in metas if m.difficulty == "medium") == 9
    assert sum(1 for m in metas if m.difficulty == "hard") == 5


def test_discover_tasks_filter_and_unknown() -> None:
    assert discover_tasks(["bf_calc_ops"]) == ["bf_calc_ops"]
    with pytest.raises(ConfigError, match="未找到任务"):
        discover_tasks(["no_such_task"])


def test_smoke_tasks_have_cassette_and_others_not() -> None:
    for tid in SMOKE_TASKS:
        assert (TASKS_DIR / tid / "cassette.json").is_file(), tid
    for tid in discover_tasks():
        if tid not in SMOKE_TASKS:
            assert not (TASKS_DIR / tid / "cassette.json").exists(), tid


# ---- init 拷贝 + .mncc.toml 注入（D3）----


def test_prepare_workdir_copies_init_and_task_not_test(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "init").mkdir(parents=True)
    (src / "init" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (src / "init" / "sub").mkdir()
    (src / "init" / "sub" / "b.txt").write_text("b", encoding="utf-8")
    (src / "task.md").write_text("任务", encoding="utf-8")
    (src / "test.py").write_text("def test(): pass\n", encoding="utf-8")

    work = tmp_path / "work"
    work.mkdir()
    prepare_workdir(src, work)
    assert (work / "a.py").read_text(encoding="utf-8") == "x = 1\n"  # 内容级拷贝
    assert (work / "sub" / "b.txt").is_file()  # 目录递归
    assert (work / "task.md").is_file()
    assert not (work / "test.py").exists()  # D2：评分文件不进场


def test_write_task_config_injects_max_turns_by_difficulty(tmp_path: Path) -> None:
    from mncc.config import parse_toml_subset

    for difficulty, expected in DIFFICULTY_MAX_TURNS.items():
        work = tmp_path / f"parse-{difficulty}"
        work.mkdir()
        write_task_config(work, TaskMeta("t", "n", "bugfix", difficulty))
        content = (work / ".mncc.toml").read_text(encoding="utf-8")
        assert f"max_turns = {expected}" in content
        # 注入的配置必须能被 mncc 自己的解析器读回（复用合并链而非手写格式）
        raw = parse_toml_subset(content)
        assert raw["max_turns"] == expected


# ---- 评分器（D1）----


def test_scoring_pytest_pass_and_fail(tmp_path: Path) -> None:
    (tmp_path / "test.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    verified, detail = run_scoring_pytest(tmp_path)
    assert verified is True and "passed" in detail

    (tmp_path / "test.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    verified, detail = run_scoring_pytest(tmp_path)
    assert verified is False and "failed" in detail


# ---- mock 模式（D5）----


def test_run_task_mock_smoke_verified() -> None:
    """smoke 任务 + cassette：进程内走完整 main() 管线，客观判分通过。"""
    result = run_task(load_meta("bf_calc_ops"), mode="mock")
    assert result.mode == "mock"
    assert result.status == "pass"
    assert result.verified is True
    assert result.exit_code == 0
    assert result.turns > 0 and result.total_tokens > 0  # stats-json 落盘被读回


def test_run_task_mock_no_cassette_skipped() -> None:
    result = run_task(load_meta("bf_str_join"), mode="mock")
    assert result.mode == "mock" and result.status == "skipped"


def test_run_mock_end_to_end_writes_results_json(tmp_path: Path, monkeypatch, capsys) -> None:
    """run(mode=mock)：results JSON 落盘、mode=mock、mock 不进汇总、退出码 0。"""
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(runner, "git_head", lambda: "abc1234")

    exit_code = run("mock", tasks=SMOKE_TASKS + ["bf_str_join"], label="冒烟")

    files = list(tmp_path.glob("mock-*.json"))
    assert len(files) == 1 and exit_code == 0
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["mode"] == "mock" and payload["label"] == "冒烟"
    assert payload["git_head"] == "abc1234"
    by_id = {t["task_id"]: t for t in payload["tasks"]}
    assert by_id["bf_calc_ops"]["status"] == "pass"
    assert by_id["bf_calc_ops"]["verified"] is True
    assert by_id["bf_str_join"]["status"] == "skipped"
    assert payload["summary"] == {}  # 无 real 结果：mock 不进跑分统计（D5）
    captured = capsys.readouterr()
    assert "无 real 结果" in captured.out


# ---- real 模式控制流（subprocess 打桩，无真实 API）----


def _fake_subprocess_run(monkeypatch, tmp_path: Path, *, agent_effect: str) -> None:
    """把 runner 的 subprocess.run 换成可编程替身，覆盖 real 模式全流程。"""

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[1:3] == ["-m", "mncc"]:
            if agent_effect == "timeout":
                raise subprocess.TimeoutExpired("mncc", 999)
            stats_path = Path(cmd[cmd.index("--stats-json") + 1])
            stats_path.write_text(
                json.dumps(
                    {"status": "completed", "turns": 3, "prompt_tokens": 100,
                     "completion_tokens": 40, "total_tokens": 140, "elapsed": 1.5,
                     "chars": 10}
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if isinstance(cmd, list) and cmd[1:3] == ["-m", "pytest"]:
            ok = agent_effect == "pass"
            return subprocess.CompletedProcess(
                cmd, 0 if ok else 1,
                "2 passed in 0.1s" if ok else "1 failed in 0.1s", "",
            )
        raise AssertionError(f"未预期的子进程调用：{cmd}")

    monkeypatch.setattr("bench.runner.subprocess.run", fake_run)


def test_run_task_real_pipeline_via_stubbed_subprocess(
    tmp_path: Path, monkeypatch
) -> None:
    task = load_meta("bf_calc_ops")
    _fake_subprocess_run(monkeypatch, tmp_path, agent_effect="pass")
    result = run_task(task, mode="real")
    assert result.mode == "real" and result.status == "pass"
    assert result.exit_code == 0 and result.verified is True
    assert result.turns == 3 and result.total_tokens == 140


def test_run_task_real_timeout(tmp_path: Path, monkeypatch) -> None:
    _fake_subprocess_run(monkeypatch, tmp_path, agent_effect="timeout")
    result = run_task(load_meta("bf_calc_ops"), mode="real")
    assert result.status == "timeout" and result.verified is False
    assert "强杀" in result.error


def test_run_task_real_exit_zero_but_tests_fail(tmp_path: Path, monkeypatch) -> None:
    """D1 核心：模型自报成功（退出码 0）但评分 pytest 挂 → 仍判 fail。"""
    _fake_subprocess_run(monkeypatch, tmp_path, agent_effect="fail")
    result = run_task(load_meta("bf_calc_ops"), mode="real")
    assert result.exit_code == 0  # 只记录不判分
    assert result.status == "fail" and result.verified is False  # 客观判分


# ---- 汇总聚合与报告 ----

META = {
    "a": TaskMeta("a", "n", "bugfix", "easy"),
    "b": TaskMeta("b", "n", "feature", "hard"),
    "c": TaskMeta("c", "n", "refactor", "medium"),
    "d": TaskMeta("d", "n", "testwrite", "easy"),
}


def _result(task_id: str, status: str) -> TaskResult:
    return TaskResult(task_id=task_id, mode="real", status=status, exit_code=0,
                      verified=status == "pass", pytest_detail="")


def test_aggregate_overall_and_dimensions() -> None:
    results = [
        _result("a", "pass"),
        _result("b", "fail"),
        _result("c", "pass"),
        _result("d", "pass"),
    ]
    summary = aggregate(results, META)
    assert summary["total"] == 4 and summary["pass"] == 3
    assert summary["pass_rate"] == 0.75
    assert summary["by_difficulty"]["easy"]["pass_rate"] == 1.0
    assert summary["by_difficulty"]["hard"]["pass"] == 0
    assert summary["by_category"]["feature"]["total"] == 1
    assert summary["total_tokens"] == 0 and summary["elapsed"] == 0.0


def test_report_markdown_table_content() -> None:
    fake = {
        "run_id": "real-20260902-100000",
        "mode": "real",
        "label": "基线",
        "git_head": "abc1234",
        "tasks": [asdict_like(_result("a", "pass")), asdict_like(_result("b", "fail"))],
        "summary": aggregate([_result("a", "pass"), _result("b", "fail")], META),
    }
    md = report.markdown_table(fake)
    assert "real-20260902-100000" in md
    assert "| a |" in md and "| b |" in md
    assert "50.0%" in md and "pass_rate **50.0%**" in md
    assert "difficulty" not in md.lower() or "easy" in md  # 维度表带 easy 行


def test_report_compare_table_shows_deltas() -> None:
    old = {
        "run_id": "real-old", "mode": "real", "label": "", "git_head": "a",
        "tasks": [asdict_like(_result("a", "fail")), asdict_like(_result("b", "pass"))],
        "summary": aggregate([_result("a", "fail"), _result("b", "pass")], META),
    }
    new = {
        "run_id": "real-new", "mode": "real", "label": "迭代", "git_head": "b",
        "tasks": [asdict_like(_result("a", "pass")), asdict_like(_result("b", "pass"))],
        "summary": aggregate([_result("a", "pass"), _result("b", "pass")], META),
    }
    md = report.compare_table(old, new)
    assert "real-old" in md and "real-new" in md
    assert "50.0% → 100.0%" in md
    assert "| a | 失败 | 通过 |" in md


def test_report_load_run_raises_on_missing(tmp_path: Path, capsys) -> None:
    assert report.main([str(tmp_path / "nope.json")]) == 1
    assert "读取结果失败" in capsys.readouterr().err


def asdict_like(result: TaskResult) -> dict:
    """构造与 runner 落盘同结构的任务明细（含难度/类别——落盘时缺少，报告从 meta 读）。

    注：runner 落盘的 task 明细来自 TaskResult（无难度/类别），真实结果文件由
    load_run+meta 联用补齐；这里直接给出带维度的结构以便断言 markdown 输出。
    """
    data = {
        "task_id": result.task_id,
        "mode": result.mode,
        "status": result.status,
        "exit_code": result.exit_code,
        "verified": result.verified,
        "pytest_detail": result.pytest_detail,
        "turns": result.turns,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        "elapsed": result.elapsed,
        "error": result.error,
    }
    meta = META[result.task_id]
    data.update(difficulty=meta.difficulty, category=meta.category)
    return data
