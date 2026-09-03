"""token 估算启发式（§4.5）与 M4 两级压缩：TokenEstimator 校准、ContextManager。"""

from __future__ import annotations

import pytest

from mncc.agent.context import (
    ContextManager,
    TokenEstimator,
    estimate_tokens,
)
from mncc.llm.client import CompletionResult, LLMError, Message
from mncc.prompts.system import SUMMARY_PROMPT


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("abcd", 1),  # 4 英文字符 ≈ 1 token
        ("abc", 1),  # 不足 4 向上取整
        ("你好", 2),  # 中文 1 字 ≈ 1 token
        ("你好abcd", 3),  # 混合：2 + 1
        ("！", 1),  # 全角标点按 CJK 计
    ],
)
def test_estimate_tokens(text: str, expected: int) -> None:
    assert estimate_tokens(text) == expected


# ---- M4：TokenEstimator 在线校准（D1）----


def test_estimator_default_matches_module_function() -> None:
    assert TokenEstimator().estimate("你好abcd") == estimate_tokens("你好abcd")
    assert TokenEstimator().divisor == 4.0


def test_estimator_observe_shifts_divisor_toward_true_density() -> None:
    """模拟英文真实密度 3 字符/token：divisor 应向 4 * (1333/1000) 靠拢。"""
    est = TokenEstimator()
    text = "a" * 4000
    before = est.estimate(text)
    assert before == 1000
    est.observe(before, 1333)
    # 0.8*4 + 0.2*(4*1333/1000) = 4.2664
    assert est.divisor == pytest.approx(4.2664, abs=1e-3)
    assert est.estimate(text) < before  # 估算向真实变小方向收敛


def test_estimator_observe_ignores_nonpositive() -> None:
    est = TokenEstimator()
    est.observe(0, 100)
    est.observe(100, 0)
    assert est.divisor == 4.0


# ---- M4：ContextManager 触发/估算 ----


def _cm(limit: int = 100, threshold: float = 0.8) -> ContextManager:
    return ContextManager(
        model_context_limit=limit, compact_threshold=threshold, summary_max_tokens=100
    )


def test_should_compact_boundary() -> None:
    cm = _cm()  # 阈值 80（int(100*0.8)）
    at_80 = [{"role": "system", "content": "a" * 320}]  # 恰 80 tokens
    below_79 = [{"role": "system", "content": "a" * 316}]  # 79 tokens
    assert cm.should_compact(at_80) is True
    assert cm.should_compact(below_79) is False


def test_estimate_messages_counts_tool_arguments() -> None:
    assert (
        _cm().estimate_messages(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "t1",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": "x" * 400},
                        }
                    ],
                }
            ]
        )
        == 100
    )


# ---- M4：L1 截断（D6）----


def test_truncate_tool_output_keeps_head_tail_and_marker() -> None:
    cm = _cm()
    text = "x" * 20_000
    out = cm.truncate_tool_output(text)
    assert len(out) == 12_000 + len("…[中间省略 4000 字符]…") + 4_000
    assert out.startswith("x" * 12_000)
    assert out.endswith("x" * 4_000)
    assert "中间省略 4000 字符" in out


def test_truncate_tool_output_passes_short_text_through() -> None:
    cm = _cm()
    assert cm.truncate_tool_output("短文本") == "短文本"
    assert len(cm.truncate_tool_output("x" * 16_000)) == 16_000  # 恰在上限不截断


# ---- M4：L2 压缩（D3/D4/D5）----


class FakeSummaryClient:
    """只实现 complete()：ContextManager.compact 的依赖面。"""

    model = "fake"

    def __init__(self, reply: str | Exception = "对话摘要") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def complete(self, messages: list[Message], max_tokens: int | None = None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        if isinstance(self.reply, Exception):
            raise self.reply
        return CompletionResult(self.reply)


def _tool_message(cid: str, content: str) -> Message:
    return {"role": "tool", "tool_call_id": cid, "content": content}


def _tool_calls_message(cid: str, name: str = "echo") -> Message:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
        ],
    }


def test_compact_keeps_system_summary_and_last_two_rounds() -> None:
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "q1"},
        _tool_calls_message("t1"),
        _tool_message("t1", "r1"),
        {"role": "user", "content": "q2"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "t2",
                    "type": "function",
                    "function": {"name": "echo", "arguments": "{}"},
                }
            ],
        },
        _tool_message("t2", "r2"),
        {"role": "user", "content": "q3"},
        _tool_calls_message("t3"),
        _tool_message("t3", "r3"),
        {"role": "user", "content": "q4"},
        {"role": "assistant", "content": "回答4"},
    ]
    client = FakeSummaryClient()
    new_messages, report = _cm().compact(messages, client)

    # system 守位 + 摘要为 user 消息 + 最近 2 原子轮完整保留（工具轮成组）
    assert new_messages == [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "对话摘要"},
        *messages[-5:],  # q3 轮（user+assistant(tc)+tool）与 q4 轮
    ]
    assert new_messages[0] == messages[0]
    # tool 消息未与工具轮分离
    tool_roles = [m for m in new_messages if m["role"] == "tool"]
    assert tool_roles == [_tool_message("t3", "r3")]
    # 摘要请求：system=SUMMARY_PROMPT、user=旧消息文本、max_tokens 透传
    call = client.calls[0]
    assert call["messages"][0] == {"role": "system", "content": SUMMARY_PROMPT}
    assert call["messages"][1]["role"] == "user"
    assert "q1" in call["messages"][1]["content"]
    assert "q2" in call["messages"][1]["content"]
    assert "r2" in call["messages"][1]["content"]
    assert call["max_tokens"] == 100
    # 报告
    assert report.degraded is False
    assert report.summary_chars == len("对话摘要")
    assert report.before_tokens == _cm().estimate_messages(messages)
    assert report.after_tokens == _cm().estimate_messages(new_messages)
    assert report.after_tokens < report.before_tokens


def test_compact_no_op_when_history_within_two_rounds() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    client = FakeSummaryClient()
    new_messages, report = _cm().compact(messages, client)
    assert new_messages == messages
    assert report.before_tokens == report.after_tokens
    assert report.summary_chars == 0
    assert client.calls == []  # 无可压缩内容时不发起摘要请求


def test_compact_truncates_oversized_summary_input() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x" * 20_000},
        {"role": "assistant", "content": "y" * 20_000},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3"},
    ]
    client = FakeSummaryClient()
    _cm().compact(messages, client)
    sent = client.calls[0]["messages"][1]["content"]
    assert len(sent) < 16_100  # 前 12000 + 省略标记 + 后 4000
    assert "中间省略" in sent
    assert sent.endswith("y" * 4_000)


def test_compact_degraded_on_llm_error_trims_oldest() -> None:
    cm = _cm(limit=1000)  # 阈值 800
    messages = [{"role": "system", "content": "s"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "a" * 320}
        for i in range(12)  # 12 × 80 tokens = 960 ≥ 800
    ]
    client = FakeSummaryClient(LLMError("网络失败"))
    new_messages, report = cm.compact(messages, client)
    assert report.degraded is True
    assert new_messages[0] == messages[0]  # system 守位
    assert len(new_messages) < len(messages)  # 只删最老：尾部原样保留
    assert new_messages[1:] == messages[-(len(new_messages) - 1) :]
    assert cm.estimate_messages(new_messages) < 800  # 低于阈值


def test_compact_degraded_on_empty_summary() -> None:
    cm = _cm(limit=1000)
    messages = [{"role": "system", "content": "s"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "a" * 320} for i in range(12)
    ]
    client = FakeSummaryClient("")
    _new, report = cm.compact(messages, client)
    assert report.degraded is True
    assert report.summary_chars == 0
