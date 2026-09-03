# M4 复盘：两级上下文压缩 + token 计量在线校准 + 配置补全

> 写作日期：2026-09-03。事实来源：`M4_DESIGN.md`、`mncc/agent/context.py`、
> `mncc/agent/loop.py`、`tests/`（代码可查证）、记忆文件 topics.md（20260903）、
> 上一会话实现记录。`M4_DESIGN.md` 未设难点章节，踩坑部分按代码痕迹与实现会话
> 还原，已标注「（分析）」「（对话记录）」。
> 「对话记录」指开发时的一手对话过程，未落盘于 git/文档，不可独立核验；以其为唯一依据的表述视同（分析）。

## 1. 目标与范围

把上下文从"用光就 /clear"升级为两级防线：**L1** 单条工具输出进历史前的体积兜底
（16 000 字符，前 12 000 + 省略标记 + 后 4 000）；**L2** 估算用量达窗口 80% 时
auto-compact（system + 摘要 + 最近 2 轮，摘要经非流式 summarize）；外加 token
在线校准（英文密度系数随 usage 做 EMA）与 3 个压缩配置项。不做：会话持久化
/save /resume、Anthropic 协议、git 集成；且 **tools/、safety/、流式路径一律不碰**——
先不动现有 200 项测试契约，再做加法（`M4_DESIGN.md` 边界）。

## 2. 开发过程

顺序：**复述 D1–D7 等用户确认 → 抽象层 → 叶子模块 → 编排 → 装配 → 测试**。

1. 按 §8 开发协议先复述设计、用户确认后才动手——起点是改 `run_agent_loop` 签名这类
   高破坏面改动，先对齐再动手。
2. 先给 `llm/client.py` 加非流式 `complete()` 抽象与 `CompletionResult`，再实现
   `openai_compat.py`（`stream=False`，复用异常翻译）。**为什么先抽象层**：`compact`
   依赖 summarize，没有 complete() 后面全是死代码；抽象方法一加测试双佬必编译失败，
   早暴露早补。
3. `agent/context.py` 重构：保留模块级 `estimate_tokens` 原签名（render/loop/测试
   直接 import 的既有契约），新增 `TokenEstimator`、`ContextManager`、`CompactReport`；
   `SUMMARY_PROMPT` 与 3 配置项（含 TOML 小数解析）同步补齐。
4. `loop.py` 编排：守卫区后触发 `compact_session`（auto/手动共用一份实现）、
   `context.estimator.observe(...)` 校准、4 条工具回填路径统一过 L1。
5. 测试随 4 步推进，扩展约 32 项；**先跑旧测试确认契约未破坏，再补新断言**。
   收尾 `verify_m3.py` 追加第 2.5 层 M4 冒烟。

## 3. 关键设计决策

| 决策 | 内容 | 被放弃的替代方案 |
|---|---|---|
| D1 只校英文系数 | divisor EMA：`0.8*d + 0.2*(4*ratio)`，初值 4.0；不动 CJK（误差稳） | 直接覆盖（单次请求 prompt 组成差异大，会震荡）；双参数校准（状态面大收敛慢） |
| D2 触发点 | 每轮模型调用**之前**（与守卫同点），`estimate >= int(limit*threshold)` | 调用后处理——请求已发出，超窗就是服务端 400，晚了 |
| D3 压缩结构 | system + **role=user** 摘要 + 最近 2 原子轮，工具轮成组保留 | 摘要开 role=system（兼容端点容忍度不一）；只留 1 轮（立刻重复上一步）；tool 消息散落轮外（协议非法） |
| D4 摘要请求 | 非流式 `complete()`，不带 tools，`max_tokens=summary_max_tokens` | 复用 `stream()` 聚合（路径最长）；带 tools（模型想调工具浪费轮次） |
| D5 失败降级 | summarize 失败/空回复 → 截断最老消息，`degraded=True` | abort 任务——上下文最危险时任务最不该死 |
| D6 L1 定位 | 16 000 字符体积兜底，不替代工具自身语义截断 | 只靠语义限额（单次意外大输出仍可撑爆，两类职责不同） |
| D7 配置语法 | `compact_threshold` 用 float（`0 < t <= 1`），解析器加小数分支 | 0–100 整数（与代码 `* threshold` 不同构，多一层换算） |

共性：每条都在"安全/成本/可维护"上取点，且把"延迟决策"前移——压缩不可逆，
容错与降级边界先拍死，实现只执行。

## 4. 难点与踩坑

- **抽象方法破坏 LLMClient 子类双佬**（对话记录；`tests/test_loop.py`、
  `test_cli.py`、`test_context.py` 含 `def complete` 可查证）：`complete()` 一加，
  ScriptedClient/DummyClient/ReplyClient 等全部 TypeError 实例化失败，属**正常信号**——
  先补 `complete()` 再断言（FakeSummaryClient 非子类、不受影响）。
- **observe 挂载错位**（对话记录；`loop.py:244` 可查证）：先写 `context.observe`，
  而设计里 observe 属于 `TokenEstimator`——AttributeError 当轮被测试抓到，改
  `context.estimator.observe`。防复发：实现对照设计签名写。
- **L1 覆盖全部回填路径**（分析；`loop.py:262/277/282/289` 四处 truncate 可查证）：
  正常回填、无工具会话、中断占位、拒绝占位各一条——只改主路径会漏闸。
- **阈值边界**（分析；`context.py:141-146`）：`int(limit*threshold)` 而非 round，
  保证"恰 80% 触发"的 `>=` 语义不被浮点舍入偷走。
- **原子轮切分**（对话记录）：从尾部向前收 tool 组，必须确认其上是带 `tool_calls`
  的 assistant 再组队，否则结构非法——D3 协议合法性的落地。

## 5. 验收与数据

- 测试：M3 末 200 项 → **M4 末 232 项全绿（新增 32 项）**，ruff 零报错
  （`M4_DESIGN.md` 状态行）。
- `verify_m3.py` 第 1/2/2.5 层实测全 PASS（第 2.5 层为 M4 冒烟：配置/触发边界/L1/
  L2 结构/降级），无 API。
- **本机真实验收未完成即进 M5**（记忆 topics.md 20260903）：场景 3（大文件逼近窗口
  观察 auto-compact）、REPL `/compact`/`/context`、verify 第 3 层需真实 API，M4 收尾
  未跑，真实环境验证由 M5 bench 全量跑分承担（README：基线 20/20，100%）。

## 6. 一分钟面试讲述版

M4 给上下文上了两级防线。L1 是体积兜底：任何工具输出进历史前压到 16k 字符，
前 12k 加省略标记加后 4k，防单次大输出撑爆窗口，但不替代工具自身的语义截断。
L2 是摘要压缩：估算用量到窗口 80% 时发起一次非流式 summarize，把旧历史压成
500 token 中文摘要，保留 system 和最近两个"原子轮"——工具轮必须成组保留，
否则 tool 消息与 assistant 分家，协议就非法了。压缩失败降级为截断最老消息
而非中止任务，因为上下文最危险时任务最不该死。token 计量做在线校准：每条
响应的 usage 与本地估算配对，用 EMA 只校英文密度系数——中文误差本就稳定，
只校一个参数收敛快、好解释。全部可配置、旧配置直接兼容，测试 200→232，
无真实 API 也全绿。

## 7. 延伸与建议

- **重做会改什么**（分析）：EMA 的 0.8/0.2 是拍脑袋权重，可用真实跑分 usage 对
  离线拟合再定初值；/context 同时维护展示（静态）与决策（校准）两套口径，重做会
  统一口径让 UI 也吃 `estimator.divisor`，少一处漂移。auto-compact 阈值可按剩余
  token 预算动态化——摘要本身也烧一次请求。
- **面试追问**："为什么留 2 轮不留 1 轮？"→ 模型要看"刚做了什么、结果如何"；
  "为什么不用 role=system 放摘要？"→ system 协议单条，兼容端点容忍度不一；
  "为什么不用 tiktoken？"→ 依赖白名单没有它，启发式+校准误差可控；"什么场景
  不该压缩？"→ 短会话、高实时、精确引用类任务（法律/科研）——面试点 §7 要主动讲。
- **后续呼应**：M5 的 `--stats-json` 让校准假设第一次面对真实数据（README 脚注：
  服务端断供后 stats 改为 graceful 失败也落盘，失败归因数据同样值钱）；也说明
  "压缩救不了模型幻觉"，评测靠客观断言兜底。