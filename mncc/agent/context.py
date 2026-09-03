"""上下文管理：token 估算（含在线校准）+ 两级压缩编排（L1 截断 + L2 auto-compact）。

M1 落地模块级 `estimate_tokens` 启发式：
- CJK 字符按 ≈1 token/字计；
- 其余字符按 ≈4 字符/token 计。

M4 在此之上新增：

- `TokenEstimator`（D1）：英语密度系数 divisor 随 API 真实 usage 在线校准。
  为什么只校英文系数：中文 1 字≈1 token 对主流 BPE 词表（GLM/DeepSeek）误差
  小且稳定，英文 4 字符/token 偏差最大；只校一个参数，状态面小、收敛快。
  用 EMA 而非直接覆盖：单次请求 prompt 组成差异大（纯代码 vs 纯中文），
  直接覆盖会震荡。
- `ContextManager`（D2/D3/D4/D5/D6）：L1 工具输出体积兜底截断 + L2 摘要压缩。
  L1（D6）与工具自身语义截断职责不同：工具截断决定"读哪些内容"，L1 只兜
  体积，防止单次意外大输出撑爆上下文。L2 压缩失败（D5）降级为截断最老消息
  而不是 abort——上下文接近爆掉时任务最不该死，能继续 > 信息完整。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..llm.client import LLMClient, LLMError, Message
from ..prompts.system import SUMMARY_PROMPT

_CJK_RE = re.compile(
    "[\u3000-\u303f"  # CJK 标点
    "\u4e00-\u9fff"  # CJK 统一表意文字
    "\u3400-\u4dbf"  # 扩展 A
    "\uff00-\uffef]"  # 全角字符
)

# L1 截断上限（D6）：前 12000 + 省略标记 + 后 4000
L1_TRUNCATE_LIMIT = 16_000
L1_TRUNCATE_HEAD = 12_000
L1_TRUNCATE_TAIL = 4_000
# 摘要输入文本上限：超过则先按 D6 样式截断，防止摘要请求本身超窗
SUMMARY_INPUT_MAX_CHARS = 30_000

_DEFAULT_DIVISOR = 4.0
_EMA_WEIGHT = 0.8


def estimate_tokens(text: str) -> int:
    """混合启发式估算 token 数（§4.5）。中文 1 字 ≈ 1 token，英文 4 字符 ≈ 1 token。

    模块级函数保持未校准静态口径：UI 状态栏、/context 展示用，口径稳定可预期。
    压缩决策用 ContextManager 内部校准后的估算——展示要稳定、决策要准确。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = len(text) - cjk
    return cjk + math.ceil(rest / 4)


def _cut_middle(text: str) -> str:
    """保留首尾、中间替换为省略标记（D6 样式）。"""
    omitted = len(text) - L1_TRUNCATE_HEAD - L1_TRUNCATE_TAIL
    return f"{text[:L1_TRUNCATE_HEAD]}…[中间省略 {omitted} 字符]…{text[-L1_TRUNCATE_TAIL:]}"


class TokenEstimator:
    """带在线校准的 token 估算器（D1）。

    estimate 口径与模块级 estimate_tokens 相同（CJK 1 字 + ceil(rest/divisor)），
    只是 divisor 随校准演化。observe 只在 estimated/actual 都为正时生效——
    usage 缺失或异常端点返回 0 时不污染校准状态。
    """

    def __init__(self) -> None:
        self._divisor = _DEFAULT_DIVISOR

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        cjk = len(_CJK_RE.findall(text))
        rest = len(text) - cjk
        return cjk + math.ceil(rest / self._divisor)

    def observe(self, estimated: int, actual: int) -> None:
        """一次配对观测：本地估算的 prompt tokens vs API 返回的真实 prompt_tokens。

        ratio = actual / estimated；divisor = EMA(4 * ratio)（初值 4.0）。
        估计偏大（ratio<1）时新目标 <4，divisor 下移、下次估算变大——向真实密度收敛。
        """
        if estimated <= 0 or actual <= 0:
            return
        ratio = actual / estimated
        self._divisor = _EMA_WEIGHT * self._divisor + (1 - _EMA_WEIGHT) * (4 * ratio)

    @property
    def divisor(self) -> float:
        """当前英语密度系数（测试与 /context 展示用）。"""
        return self._divisor


@dataclass
class CompactReport:
    """一次 L2 压缩的结果。degraded=True 表示 summarize 失败、降级为截断最老消息。"""

    before_tokens: int
    after_tokens: int
    summary_chars: int
    degraded: bool = False


class ContextManager:
    """L1 截断 + L2 压缩的编排者，持有在线校准的 TokenEstimator。"""

    def __init__(
        self,
        *,
        model_context_limit: int,
        compact_threshold: float,
        summary_max_tokens: int,
    ) -> None:
        self.model_context_limit = model_context_limit
        self.compact_threshold = compact_threshold
        self.summary_max_tokens = summary_max_tokens
        self.estimator = TokenEstimator()

    # ---- 估算与触发 ----

    def estimate_messages(self, messages: list[Message]) -> int:
        """校准后估算整段历史（与 Session.tokens_estimate 同口径：content + tool_calls 参数）。

        为什么压缩决策用校准后的数：决策需要"离真实窗口还有多远"，力求准；
        展示用未校准静态估计，口径稳定可预期。
        """
        total = 0
        for m in messages:
            total += self.estimator.estimate(str(m.get("content") or ""))
            for tc in m.get("tool_calls") or []:
                total += self.estimator.estimate(str(tc.get("function", {}).get("arguments") or ""))
        return total

    def _threshold_tokens(self) -> int:
        # 取整避免浮点边界：恰在 80% 时应触发（>= 语义）
        return int(self.model_context_limit * self.compact_threshold)

    def should_compact(self, messages: list[Message]) -> bool:
        return self.estimate_messages(messages) >= self._threshold_tokens()

    # ---- L1（D6）----

    def truncate_tool_output(self, text: str) -> str:
        """工具输出回填前的统一体积兜底。不替代工具自身语义截断。"""
        if len(text) <= L1_TRUNCATE_LIMIT:
            return text
        return _cut_middle(text)

    # ---- L2（D3/D4/D5）----

    def compact(
        self, messages: list[Message], client: LLMClient
    ) -> tuple[list[Message], CompactReport]:
        """压缩历史：system + 一条 user 摘要 + 最近 2 个原子轮。

        原子轮划分（从尾部向前）：assistant 消息连同其后的连续 tool 消息是
        一整轮——tool 消息必须留在归属轮内，否则协议结构非法（D3）。user
        消息是其轮起点，随轮保留。system 不参与摘要、原地守位。
        摘要用非流式 complete()（D4）；失败（LLMError/空回复）降级为截断最老
        消息（D5）。
        """
        before = self.estimate_messages(messages)
        old, kept = self._split_recent_rounds(messages)
        system = old[:1] if old and old[0].get("role") == "system" else []
        old_rest = old[len(system) :]
        if not old_rest:
            # 无可压缩内容（历史不超过 2 轮）：原样返回
            return list(messages), CompactReport(before, before, 0)

        summary_text = self._messages_to_text(old_rest)
        if len(summary_text) > SUMMARY_INPUT_MAX_CHARS:
            summary_text = _cut_middle(summary_text)
        try:
            result = client.complete(
                [
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": summary_text},
                ],
                max_tokens=self.summary_max_tokens,
            )
        except LLMError:
            return self._degraded(messages, before)
        summary = result.content.strip()
        if not summary:
            return self._degraded(messages, before)

        new_messages = system + [{"role": "user", "content": summary}] + kept
        return new_messages, CompactReport(
            before, self.estimate_messages(new_messages), len(summary)
        )

    def _degraded(
        self, messages: list[Message], before: int
    ) -> tuple[list[Message], CompactReport]:
        """D5 降级：保留 system，从最旧的非 system 消息开始逐条删除。"""
        system = messages[:1] if messages and messages[0].get("role") == "system" else []
        rest = list(messages[len(system) :])
        while rest and self.estimate_messages(system + rest) >= self._threshold_tokens():
            del rest[0]
        trimmed = system + rest
        return trimmed, CompactReport(before, self.estimate_messages(trimmed), 0, degraded=True)

    def _split_recent_rounds(self, messages: list[Message]) -> tuple[list[Message], list[Message]]:
        """切成 (旧消息=进摘要, 保留=最近 2 原子轮)。"""
        kept: list[Message] = []
        rounds = 0
        i = len(messages) - 1
        while i >= 0 and rounds < 2:
            role = messages[i].get("role")
            if role == "tool":
                group = [messages[i]]
                i -= 1
                while i >= 0 and messages[i].get("role") == "tool":
                    group.insert(0, messages[i])
                    i -= 1
                if (
                    i >= 0
                    and messages[i].get("role") == "assistant"
                    and messages[i].get("tool_calls")
                ):
                    group.insert(0, messages[i])
                    i -= 1
                if i >= 0 and messages[i].get("role") == "user":
                    group.insert(0, messages[i])
                    i -= 1
                kept = group + kept
                rounds += 1
            elif role == "assistant":
                kept.insert(0, messages[i])
                i -= 1
                if i >= 0 and messages[i].get("role") == "user":
                    kept.insert(0, messages[i])
                    i -= 1
                rounds += 1
            elif role == "user":
                # 孤立 user（正常流程罕见）：保留但不消耗轮配额
                kept.insert(0, messages[i])
                i -= 1
            else:
                break  # system 等：历史头部不再向前
        return messages[: i + 1], kept

    def _messages_to_text(self, messages: list[Message]) -> str:
        parts: list[str] = []
        for m in messages:
            content = str(m.get("content") or "").strip()
            if content:
                parts.append(f"{m.get('role')}: {content}")
        return "\n".join(parts)
