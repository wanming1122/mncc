# M4 设计文档：两级上下文压缩 + token 计量在线校准 + 配置补全

> 状态：设计已确认，编码完成（2026-09-02）。232 项测试全绿（新增 32 项），ruff 零报错。
> **本文件是 M4 开发的唯一事实源**——新对话中的开发者
> 请先通读，按 §8「开发协议」执行：先向用户确认本设计（或按用户修改意见调整），
> 再开始编码，禁止跳过确认直接写码。
>
> 范围：PROMPT.md §12 M4——L1 工具输出截断、L2 auto-compact、token 计量在线校准、
> 配置系统补全（compact 相关配置项）。
> 边界：会话持久化 /save /resume 是 §7 P1（M6 后）；Anthropic 协议、git 集成不在 M4。

## 1. 现状盘点（新对话开发者必读，无需重新探索）

M1–M3 已完成 200 项测试全绿、ruff 零报错。M4 只动下列文件，**其余文件（tools/、safety/、
llm/openai_compat.py 的流式路径）一律不碰**：

| 文件 | 现状 | M4 改动 |
|---|---|---|
| `mncc/agent/context.py` | 只有模块级 `estimate_tokens(text)`（CJK 1 字≈1 token，其余 4 字符≈1 token），M1 落地 | **重构**：新增 `TokenEstimator`（在线校准）与 `ContextManager`（L1/L2），保留原模块级函数向后兼容 |
| `mncc/agent/loop.py` | `Session(messages, total_usage)`；`run_agent_loop(client, renderer, session, registry, *, max_turns, token_budget, confirm, yolo)`；`_report_usage` 在 usage 缺失时用启发式兜底但**不回填不校准** | 加校准、auto-compact 触发、工具回填前 L1 截断 |
| `mncc/llm/client.py` | `LLMClient` 抽象仅有 `stream()`；`Usage` dataclass 有 `__add__` | 新增非流式 `complete()` 抽象方法 + `CompletionResult` |
| `mncc/llm/openai_compat.py` | OpenAI 兼容流式实现（tool_calls 分片聚合） | 实现 `complete()`（`stream=False` 同步调用），不动流式路径 |
| `mncc/config.py` | **M1 已实现 90%**：默认→全局→项目→环境变量→命令行合并链、TOML 子集解析（字符串/整数/布尔/字符串数组）、`api_key_env`、`max_turns`、`token_budget` 均有且校验齐全 | 只加 3 个配置项 + 解析器支持小数 |
| `mncc/prompts/system.py` | `SYSTEM_PROMPT`（§9 完整版） | 加 `SUMMARY_PROMPT`（摘要生成专用） |
| `mncc/ui/render.py` | `context_view()` 末尾提示"M4 实现"；无压缩展示 | 新增 `compact()` 对比面板；`context_view` 更新文案 |
| `mncc/cli.py` | `/compact` 是 stub（handle_slash 返回提示"将在 M4 实现"）；`handle_slash(line, *, session, client, renderer)` | `/compact` 接真实现；handle_slash 签名加 `context` 参数 |
| `tests/` | 200 项全绿 | 扩展 test_context/test_loop/test_config/test_cli/test_openai_compat，**已有断言不回归** |
| `README.md`、`M3_DESIGN.md` | — | README 路线图勾选 M3、状态改 M4（开发完成后）；M4_DESIGN.md 状态行更新 |

关键既有契约（必须遵守）：

- `run_agent_loop` 被大量测试以现有参数调用 → **新参数必须带默认值**；
- `estimate_tokens` 被 `mncc/ui/render.py`、`mncc/agent/loop.py`、`tests/test_context.py`
  直接 import → 保留模块级函数签名不变；
- Python 3.10（ruff target py310）：f-string 表达式内不允许反斜杠转义；
- ruff：line-length 100，select E/F/W/I/UP/B；代码注释解释"为什么"不解释"是什么"；
- 消息结构合法性：`session.messages[0]` 恒为 system（`Session.reset` 依赖此约定）。

## 2. 已拍板的决策（含理由，面试点）

**D1 校准只校英语密度系数，不动 CJK 系数。** `estimate_tokens` 的两个系数里，英文
4 字符/token 偏差最大（BPE 对常见词合并激进，实际往往 3 字符/token），中文 1 字≈1 token
对主流 BPE 词表（GLM/DeepSeek）误差小且稳定。只校一个参数，状态面小、收敛快、解释简单。
校准方式：每次 API 响应带 usage 时，`ratio = usage.prompt_tokens / 本地估算值`，
用指数滑动平均更新：`divisor = 0.8 * divisor + 0.2 * (4 * ratio)`（初值 4.0）。
为什么 EMA 而非直接覆盖：单次请求的 prompt 组成差异大（纯代码 vs 纯中文），直接覆盖
会震荡，平均后才是稳定密度。

**D2 auto-compact 触发在每轮模型调用之前**（与轮数/预算守卫同一点），条件
`estimate(tokens) >= model_context_limit * compact_threshold`。为什么在这里：
与现有守卫结构一致，且在"下一轮请求发出前"做决策——请求发出去超限就晚了（服务端
直接 400/截断）。压缩后 messages 变短，循环继续。

**D3 压缩后消息结构：system + 一条摘要（role=user）+ 最近 2 轮（user/assistant 或
含 tool 消息的工具轮）。** 摘要不新开 role=system 消息——OpenAI 协议限定 system 单条，
GLM/DeepSeek 兼容端点对多条 system 的容忍度不一，用 user 消息最稳（"以下是此前对话的
摘要，请作为已知背景继续任务"）。保留最近 2 轮而不是 1 轮：模型需要看到"刚做了什么、
结果如何"，否则摘除后立刻重复上一步动作。注意工具轮的原子里 assisted(tool_calls)+tool
消息必须成组保留，**不能把 tool 消息留在组外**（协议要求每个 tool_call 有对应回填）。

**D4 摘要生成与主循环对话分别请求：`summarize` 用非流式 `complete()`，不带 tools
参数，`max_tokens=summary_max_tokens`。** 为什么在 client 抽象加 non-streaming 方法：
摘要不需要 UI 流式渲染（内部操作），同步一次拿结果代码路径最短；而且这是第二个
"Client 能力面"（stream vs complete），§7 未来接 Anthropic 时两种实现都做就是抽象
价值的又一个证明。为什么不带 tools：摘要请求是纯文本任务，带 tools 会让模型想
调工具、浪费轮次。摘要文本超长时（模型不听 max_tokens 的罕见情况）按 D6 截断。

**D5 压缩失败降级为"截断最老消息"而非中止任务。** auto-compact 触发时上下文已经
接近爆掉，此时如果 summarize 调用失败（网络抖动/限流）就 abort 任务，会让任务在
最不该失败的地方失败。降级路径：保留 system，从最旧的非 system 消息开始逐条删除
直到估算低于阈值；hint 面板告知"压缩失败，已降级为截断最老消息"。截断丢失信息但
保住任务继续——两害相权，能继续 > 信息完整。

**D6 L1 截断是"工具输出回填前"的统一第二道闸（上限 16000 字符，保留首尾），
不替代工具自身截断。** 工具内部（read_file 2000 行、grep 100 条、run_command 8k）
是**语义限额**（读了哪些内容由工具决定）；L1 是**体积兜底**（任何工具输出进入
历史前统一压到 16000 字符内，防止单次意外大输出直接撑爆上下文）。两者职责不同，
保留各自存在。截断格式：前 12000 + `…[中间省略 N 字符]…` + 后 4000。

**D7 配置新增三项且全部可被旧配置文件忽略。** 新字段有默认值，`load_config` 的
未知键检查只挡多余键、不要求必须出现 → 用户已有的 `~/.mncc/config.toml` 无需改。
新增：
- `model_context_limit: int = 128_000`（模型上下文窗口上限；GLM-4.6 为 128k，
  用 DeepSeek 时用户自己改小）
- `compact_threshold: float = 0.8`（触发压缩的比例，校验 0 < t <= 1）
- `summary_max_tokens: int = 500`（摘要输出上限 token）

TOML 子集解析器需新增小数支持（现只支持整数），加 `_FLOAT_RE`。阈值为什么用
float 而不是 0–100 整数：0.8 与 code 里 `* threshold` 直接对应，配置与实现同构，
少一层换算就是少一个出错点。

## 3. 模块设计与签名

### 3.1 `mncc/agent/context.py`（重构）

```python
# ---- 保留（向后兼容，不动）----
def estimate_tokens(text: str) -> int: ...  # 现有实现原样保留


# ---- 新增 ----
class TokenEstimator:
    """带在线校准的估算器。estimate 时：cjk + ceil(rest / divisor)，divisor 随校准演化。"""

    def __init__(self) -> None: ...  # divisor = 4.0
    def estimate(self, text: str) -> int: ...
    def observe(self, estimated: int, actual: int) -> None:
        ...
        # 一次配对观测：「本地估算的 prompt tokens」vs「API 返回的真实 prompt_tokens」。
        # ratio = actual / estimated（estimated>0 时）；divisor = 0.8*divisor + 0.2*(4*ratio)，见 D1。
        # loop 在每轮结束时调用 ctx.observe(prompt_est, completed.usage.prompt_tokens)

    @property
    def divisor(self) -> float: ...  # 测试与 /context 展示用


class ContextManager:
    """L1 截断 + L2 压缩的编排者。持有 TokenEstimator。"""

    def __init__(
        self, *, model_context_limit: int, compact_threshold: float, summary_max_tokens: int
    ) -> None: ...
    def estimate_messages(self, messages: list[Message]) -> int:
        ...
        # 与 Session.tokens_estimate 同口径：所有 content + tool_calls arguments

    def should_compact(self, messages: list[Message]) -> bool: ...
    def compact(
        self, messages: list[Message], client: LLMClient
    ) -> tuple[list[Message], CompactReport]:
        ...
        # 内部：1) 找分界点（保留 system + 最近 2 原子轮）
        #      2) 旧消息文本拼给 complete() 的 SUMMARY_PROMPT
        #      3) 成功 → [system, 摘要 user 消息, *最近两轮]
        #      4) 失败（LLMError/空回复）→ 按 D5 截断最老消息，report.degraded=True

    def truncate_tool_output(self, text: str) -> str: ...  # L1（D6）


@dataclass
class CompactReport:
    before_tokens: int  # 压缩前估算
    after_tokens: int  # 压缩后估算
    summary_chars: int  # 生成的摘要字符数
    degraded: bool = False  # True=summarize 失败降级为截断
```

"原子轮"的切分算法（compact 内部，新对话实现时参考）：从 messages 末尾向前，
```python
# 轮边界：assistant（可能带 tool_calls）→ 其后的连续 tool 消息属于同一轮
# user/（assistant 无 tool_calls）是轮终点起点
```
收集 **2 个完整原子轮**后停下；之前全部进摘要。摘要请求消息：
`[{"role":"system","content":SUMMARY_PROMPT},{"role":"user","content":待摘要文本}]`。
摘要文本若 > 30000 字符先按 D6 样式截断再请求（防止摘要请求本身超窗）。

### 3.2 `mncc/llm/client.py`（增量）

```python
@dataclass(frozen=True)
class CompletionResult:
    content: str
    usage: Usage | None = None


class LLMClient(ABC):
    @abstractmethod
    def complete(self, messages: list[Message], max_tokens: int | None = None) -> CompletionResult:
        """非流式补全（summarize 等内部任务用）。失败抛 LLMError 子类。"""
        raise NotImplementedError
```

### 3.3 `mncc/llm/openai_compat.py`（增量）

`complete()` 用底层 client 的 `chat.completions.create(stream=False)`，取
`choices[0].message.content` 与 usage；`max_tokens` 透传；空 content 返回 `""`
（由 ContextManager 判空降级）。**流式路径一行不动。**

### 3.4 `mncc/agent/loop.py`（增量）

```python
def run_agent_loop(client, renderer, session, registry=None, *,
                   max_turns=..., token_budget=..., confirm=..., yolo=...,
                   context: ContextManager | None = None) -> LoopResult:
```

改动点（都在现有函数内，不拆函数）：

1. 函数开头：`context = context or ContextManager(自动档：limit=128000, 0.8, 500)`
   （自动档保证现有测试不带参数也能跑且不触发压缩）；
2. 每轮守卫区后、`client.stream` 前：
   ```python
   if context.should_compact(session.messages):
       session.messages, report = context.compact(session.messages, client)
       renderer.compact(report)  # 打印 before→after 对比 + 是否降级
   ```
3. 流结束拿到 `completed.usage` 后：
   ```python
   if completed.usage is not None:
       session.add_usage(completed.usage)
       context.observe(prompt_est, completed.usage.prompt_tokens)  # 校准
   ```
   `prompt_est` 是本轮 stream 前的 `session.tokens_estimate()` 快照（现有变量）。
   口径约定（面试点）：`Session.tokens_estimate` 保留**未校准**静态估计（UI 状态栏、
   /context 展示用，口径稳定可预期）；压缩决策用 ContextManager 内部**校准后**的
   `estimate_messages`（需要的是"离真实窗口还有多远"，力求准）。两套口径职责不同：
   展示要稳定、决策要准确。
4. 工具回填前统一截断：`session.add_tool_message(tc.id, context.truncate_tool_output(result.output))`
   （现有 `session.add_tool_message(tc.id, result.output)` 替换，占位/拒绝类消息
   也走 truncate 无害）。

`/compact` 手动触发复用什么：手动压缩不在 loop 内（REPL 空闲时用户发起）。
新增模块级函数（loop.py 导出）：

```python
def compact_session(
    session: Session, context: ContextManager, client: LLMClient, renderer: Any
) -> bool:
    """手动压缩（/compact）。返回是否发生了压缩。"""
```

loop 的 auto 路径也调用同一个 `compact_session`（内部再包 should_compact 的判断）。
为什么抽出来：auto 与手动只差"谁发起"，动作完全相同——一份实现，避免两处漂移。

### 3.5 `mncc/prompts/system.py`（增量）

```python
SUMMARY_PROMPT = """\
你是对话历史的压缩器。把下面的对话压缩成不超过 500 token 的中文摘要。
必须保留：用户最初的任务与目标、已完成的验证结论（含测试结果）、
未完成的步骤与下一步计划、修改过的文件清单与原因、关键约束与用户偏好。
省略：过程性的工具输出细节、失败的中间尝试。只输出摘要本身，不要任何前言。"""
```

### 3.6 `mncc/config.py`（增量）

- `Config` 加 `model_context_limit: int = 128_000`、`compact_threshold: float = 0.8`、
  `summary_max_tokens: int = 500`（frozen dataclass，字段追加在末尾）
- `_parse_value` 加小数分支：`_FLOAT_RE = re.compile(r"^-?\d[\d_]*\.\d[\d_]*$")`；
  float 不支持下划线之外的怪异写法即可
- 校验：`model_context_limit`、`summary_max_tokens` 走现有正整数校验；
  `compact_threshold` 必须是 float 且 `0 < t <= 1`，否则 ConfigError

### 3.7 `mncc/ui/render.py`（增量）

```python
def compact(self, report: CompactReport) -> None:
    # Panel：压缩前 → 压缩后 token 对比 + 降级标记
def context_view(self, messages, est_tokens, *, context: ContextManager | None = None) -> None:
    # 追加一行：模型窗口 limit、校准后估算、压缩触发进度条式提示（纯文本比例即可）；
    # 删除末尾"M4 将提供"提示
```

power 提示：`context_view` 签名加关键字参数带默认值，既有调用（test_cli `handle_slash`）
不破坏。

### 3.8 `mncc/cli.py`（增量）

- `build_arg_parser` 不变（context 参数是配置驱动，不新增命令行旗标）
- `handle_slash(line, *, session, client, renderer, context)`：`/compact` 分支改为
  `compact_session(...)` + 提示文本
- REPL 与 `-p` 两处 `run_agent_loop(...)` 调用各加 `context=ctx`，其中：
  ```python
  ctx = ContextManager(
      model_context_limit=config.model_context_limit,
      compact_threshold=config.compact_threshold,
      summary_max_tokens=config.summary_max_tokens,
  )
  ```
- ScriptedClient 类测试里 DummyClient 需实现 `complete()`（测试文件同步更新，见 §4）

## 4. 测试计划（新增约 30 项；全部无真实 API）

| 文件 | 覆盖 |
|---|---|
| `tests/test_context.py` | TokenEstimator.observe 后 divisor 朝真实密度移动（fake 配对数据）；ContextManager.should_compact 阈值边界（恰 80% 触发/79% 不触发）；truncate_tool_output 首尾保留 + 中段省略标记；compact 消息结构：system 守位、摘要为 user 消息、最近 2 原子轮完整保留、tool 消息不与工具轮分离；摘要超长先截断；complete 抛 LLMError → degraded=True + 截断兜底；complete 返回空串 → degraded |
| `tests/test_loop.py` | 自动档 context 不触发（小消息回归）；auto-compact 触发（fake client：script 第一轮回复后消息构造变大→第二轮前压缩，并断言 renderer 收到 compact 调用）；校准被调用（观察 provider）；工具输出超长经 L1 截断后回填（scripted client + Echo 输出大文本）；compact_session 手动路径 |
| `tests/test_openai_compat.py` | complete() 组装正确的非流式请求（断言 create 参数 stream=False / max_tokens 透传）、解析 usage、空 content 返回 "" |
| `tests/test_config.py` | 三个新项解析、float 阈值校验边界（0 拒绝、1.0 通过、>1 拒绝）、旧配置无新键正常加载 |
| `tests/test_cli.py` | `/compact` 手动触发（DummyClient.complete 返回固定摘要）并更新 handle_slash 的调用点传 context 参数；`test_compact_is_m4_stub` **删除**（stub 消失） |
| `tests/test_tools_*.py` 等 | 不回归 |

注意：所有 ScriptedClient/DummyClient/ReplyClient 等测试双佬类都要补 `complete()`
（抽象方法强制），各测一个最小实现即可。

## 5. 验收（PROMPT §11 对应条目）

1. **场景 3**：生成 5000 行文件后 `mncc -p "读取 big.txt 全部内容并总结前 10 行"
   --yolo`：正常分页不炸；若能逼近窗口上限则观察 auto-compact 触发打印（或单测
   证明触发路径，本机验证以不炸为准）
2. **REPL `/compact`**：手动压缩生效，打印 压缩前 N → 压缩后 M tokens 对比
3. `/context` 显示校准后的估算与压缩配置（limit/阈值）
4. 现有 200 项 + 新增测试全绿；ruff 零报错
5. README：路线图 M3 勾选、M4 勾选（开发完成后）；状态行更新

## 6. 开发协议（新对话必须遵守）

1. 先向用户简要复述本设计（尤其 D1–D7 决策），用户确认或修改后再编码
2. 只动 §1 列出的文件；tools/safety/LLM 流式路径不碰
3. 保留 `estimate_tokens` 模块级签名；新参数都有默认值（既有测试零改动跑绿）
4. 每个已完成单元：先跑 `python -m pytest -q` 与 `python -m ruff check .` 再继续
5. 完成后更新本文件状态行与 README 路线图，并给出验收清单让用户跑 `python verify_m3.py`
   （该脚本第 2 层仍作为 M3 回归，M4 完成后可在脚本中追加 M4 冒烟层）

## 7. 面试点备忘（写进后续 INTERVIEW.md 的素材）

- 两级压缩的信息损失权衡：L1 丢尾部细节（可重读恢复）、L2 丢中间过程（不可逆，
  靠摘要保留决策与结论）——为什么 L2 摘要必须保留"验证结论与未完成步骤"
- token 估算误差来源：BPE 合并 vs 字符计数；中英密度差异；校准用 EMA 平滑的原因；
  prompt 组成变化导致 ratio 震荡
- auto-compact 的工程取舍：summarize 本身烧 token（约等于一次小请求），
  何时压缩是"再开一次请求省后续 N 轮"的经济账；降级路径是可用性优先
- "什么场景不该用压缩"：短会话、高交互实时性场景、摘要偏差会破坏精确引用
  （法律/科研类精确引用任务）