"""M3 一键验收脚本：`python verify_m3.py`（M4 完成后追加了第 2.5 层 M4 冒烟）

对照 PROMPT.md §11 验收场景，解决两个痛点：
- 不再需要交互式 REPL（用 `-p --yolo` 非交互 + 直接驱动真实 ToolRegistry）
- 不再需要手工搭样例仓库（脚本自动建临时目录 + 样例文件）

层次结构：
1. 逻辑证明（无 LLM）：跑 pytest + ruff。232 项单测覆盖 edit 唯一命中 / diff 预览 /
   路径守卫穿越 / 命令黑名单 / registry 分发 / 上下文压缩等全部 M3+M4 逻辑。
2. 装配冒烟（无 LLM）：直接调用 cli.build_registry 得到的真实 registry，断言六工具装配、
   黑名单拦截（--yolo 不解锁）、路径越界拒绝、edit diff 预览内容。
2.5 M4 冒烟（无 LLM）：新配置项默认值与解析、ContextManager 触发边界 / L1 截断 /
   L2 压缩结构与降级路径（fake client 回放摘要）。
3. 端到端（需真实 API）：用 `mncc -p ... --yolo` 非交互跑 §11 场景 1（修 2 个 bug），
   断言退出码 0 且终态 pytest 全绿。

无 API key 时自动跳过第 3 层（第 1/2/2.5 层已足以证明逻辑正确）；可用 --skip-llm 强制跳过。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"

_CALC_PY = """def add(a, b):
    return a + b


def sub(a, b):
    return a + b  # bug 1：减法写成了加法


def mul(a, b):
    return a * b


def div(a, b):
    if b == 0:
        return 0  # bug 2：除零没有抛 ZeroDivisionError
    return a / b
"""

_TEST_CALC_PY = """import pytest
from calc import add, sub, mul, div


def test_add():
    assert add(2, 3) == 5


def test_sub():
    assert sub(5, 3) == 2


def test_mul():
    assert mul(4, 5) == 20


def test_div():
    assert div(10, 2) == 5


def test_div_by_zero():
    with pytest.raises(ZeroDivisionError):
        div(1, 0)
"""


def run(cmd: list[str], *, cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _sub_env() -> dict[str, str]:
    # 让 `python -m mncc` 在任意 cwd（临时样例目录）下都能找到 mncc 包
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def layer_logic() -> list[tuple[str, str]]:
    """第 1 层：pytest + ruff，无 LLM、无交互。"""
    results: list[tuple[str, str]] = []
    p = run([sys.executable, "-m", "pytest", "-q"], cwd=PROJECT_ROOT)
    ok = p.returncode == 0
    tail = (p.stdout + p.stderr).strip().splitlines()
    detail = tail[-1] if tail else f"pytest 退出码 {p.returncode}"
    results.append((PASS if ok else FAIL, f"pytest 全绿（{detail}）"))

    try:
        r = run([sys.executable, "-m", "ruff", "check", "."], cwd=PROJECT_ROOT)
        rok = r.returncode == 0
        combined = (r.stdout + r.stderr).strip()
        rdetail = combined.splitlines()[-1] if combined else ""
        results.append((PASS if rok else FAIL, f"ruff 零报错（{rdetail}）"))
    except FileNotFoundError:
        results.append((SKIP, "ruff 未安装，跳过"))
    return results


def layer_registry() -> list[tuple[str, str]]:
    """第 2 层：真实 registry 装配 + 守卫拦截 + edit diff，无 LLM、无交互。"""
    from mncc.cli import build_registry

    results: list[tuple[str, str]] = []

    def confirm(_tool: object, _args: dict) -> bool:
        return True

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        registry = build_registry(root=root)

        names = registry.names()
        expected = ["read_file", "write_file", "edit_file", "list_dir", "grep", "run_command"]
        results.append((PASS if names == expected else FAIL, f"六工具装配 {names}"))

        r = registry.execute("run_command", '{"cmd": "rm -rf /"}', confirm=confirm, yolo=True)
        ok = r.is_error and "拦截" in r.output
        results.append((PASS if ok else FAIL, "命令黑名单：rm -rf 被拦（--yolo 不解锁）"))

        outside = json.dumps({"path": str(root.parent / "outside.txt")})
        r = registry.execute("read_file", outside, confirm=confirm, yolo=False)
        ok = r.is_error and "越界" in r.output
        results.append((PASS if ok else FAIL, "路径守卫：工作区外路径被拒"))

        f = root / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        edit = registry.get("edit_file")
        pv = edit.preview({"path": str(f), "old_string": "x = 1", "new_string": "x = 2"})  # type: ignore[union-attr]
        ok = "-x = 1" in pv and "+x = 2" in pv
        results.append((PASS if ok else FAIL, "edit diff 预览含 +/- 变更"))
    return results


def layer_m4() -> list[tuple[str, str]]:
    """第 2.5 层：M4 冒烟（配置项 / 触发边界 / L1 / L2 结构与降级），无 LLM。"""
    from mncc.agent.context import ContextManager
    from mncc.config import Config, load_config
    from mncc.llm.client import CompletionResult, LLMError

    results: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # D7：旧配置文件（无新键）正常加载，新项走默认值
        cfg = load_config(None, global_path=root / "g.toml", project_path=root / "nope")
        ok = (
            cfg.model_context_limit == 128_000
            and cfg.compact_threshold == 0.8
            and cfg.summary_max_tokens == 500
            and isinstance(Config().compact_threshold, float)
        )
        results.append((PASS if ok else FAIL, "配置：三项新配置默认值 + 旧配置兼容"))

        cm = ContextManager(model_context_limit=100, compact_threshold=0.8, summary_max_tokens=50)
        at_80 = [{"role": "system", "content": "a" * 320}]
        below_79 = [{"role": "system", "content": "a" * 316}]
        ok = cm.should_compact(at_80) and not cm.should_compact(below_79)
        results.append((PASS if ok else FAIL, "压缩触发：恰 80% 触发 / 79% 不触发"))

        out = cm.truncate_tool_output("x" * 20_000)
        ok = out.startswith("x" * 12_000) and out.endswith("x" * 4_000) and "省略" in out
        results.append((PASS if ok else FAIL, "L1：超长工具输出截断为首尾 + 省略标记"))

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "x" * 400},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "y" * 400},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "q3"},
            {"role": "assistant", "content": "a3"},
        ]

        class FakeClient:
            model = "fake"

            def __init__(self) -> None:
                self.calls = 0

            def complete(self, msgs, max_tokens=None):
                self.calls += 1
                return CompletionResult("任务摘要")

        ok_client = FakeClient()
        new_msgs, report = cm.compact(messages, ok_client)
        ok = (
            ok_client.calls == 1
            and new_msgs[0]["role"] == "system"
            and new_msgs[1] == {"role": "user", "content": "任务摘要"}
            and new_msgs[2:] == messages[-4:]
            and not report.degraded
            and report.after_tokens < report.before_tokens
        )
        results.append((PASS if ok else FAIL, "L2：摘要压缩结构（system 守位 + 最近两轮）"))

        class BrokenClient:
            model = "fake"

            def complete(self, msgs, max_tokens=None):
                raise LLMError("网络失败")

        _new_msgs, report = cm.compact(messages, BrokenClient())
        ok = report.degraded
        results.append((PASS if ok else FAIL, "L2 降级：summarize 失败 → degraded=True"))
    return results


def _has_api_key() -> tuple[bool, str]:
    from mncc.config import ConfigError, load_config, resolve_api_key

    try:
        cfg = load_config()
        resolve_api_key(cfg)
        return True, f"{cfg.model} @ {cfg.base_url}"
    except ConfigError as exc:
        return False, str(exc)


def layer_llm() -> tuple[str, str]:
    """第 3 层：非交互 `-p --yolo` 走真实 §11 场景 1 修 bug。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "calc.py").write_text(_CALC_PY, encoding="utf-8")
        (d / "test_calc.py").write_text(_TEST_CALC_PY, encoding="utf-8")

        task = "测试挂了，找到 bug 修复并让全部测试通过"
        p = run(
            [sys.executable, "-m", "mncc", "-p", task, "--yolo"],
            cwd=d,
            timeout=900,
        )
        if p.returncode != 0:
            return FAIL, f"mncc -p 退出码 {p.returncode}，stderr 末尾：{(p.stderr or '')[-300:]}"

        pt = run([sys.executable, "-m", "pytest", "-q", "."], cwd=d, timeout=120)
        tail = (pt.stdout + pt.stderr).strip().splitlines()
        detail = tail[-1] if tail else ""
        ok = pt.returncode == 0 and "passed" in (pt.stdout + pt.stderr)
        return (PASS if ok else FAIL, f"终态 pytest：{detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="M3 一键验收")
    parser.add_argument("--skip-llm", action="store_true", help="跳过需真实 API 的第 3 层")
    args = parser.parse_args()

    print("=" * 60)
    print("mncc M3 验收")
    print("=" * 60)

    all_results: list[tuple[str, str]] = []

    print("\n[第 1 层] 逻辑证明（pytest + ruff，无 LLM）")
    for mark, detail in layer_logic():
        all_results.append((mark, detail))
        print(f"  {mark} {detail}")

    print("\n[第 2 层] 装配冒烟（真实 registry + 守卫，无 LLM）")
    for mark, detail in layer_registry():
        all_results.append((mark, detail))
        print(f"  {mark} {detail}")

    print("\n[第 2.5 层] M4 冒烟（压缩配置 / 触发 / L1 / L2 与降级，无 LLM）")
    for mark, detail in layer_m4():
        all_results.append((mark, detail))
        print(f"  {mark} {detail}")

    print("\n[第 3 层] 端到端（-p --yolo 修 bug，需真实 API）")
    if args.skip_llm:
        print(f"  {SKIP} --skip-llm 指定跳过")
    else:
        has_key, info = _has_api_key()
        if not has_key:
            print(f"  {SKIP} 未检测到 API key（{info}）；第 1/2 层已覆盖逻辑，跳过端到端")
        else:
            print(f"  [info] 使用 {info}")
            mark, detail = layer_llm()
            all_results.append((mark, detail))
            print(f"  {mark} {detail}")

    fails = [d for m, d in all_results if m == FAIL]
    print("\n" + "=" * 60)
    if fails:
        print(f"结果：{len(fails)} 项失败")
        for d in fails:
            print(f"  失败 - {d}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
