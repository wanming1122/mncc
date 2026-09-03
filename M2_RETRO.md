# M2 复盘：工具调用闭环（read_file / write_file / run_command）

> 依据：`M2_DESIGN.md`、`PROMPT.md` §4.2/§4.3/§12、提交 `2e305aa`（M1–M5 压缩提交，
> 过程细节由开发会话记录还原）、`INTERVIEW.md` 考点 2/3。M2 无独立提交，代码归属
> 以设计文档与测试文件为准。
> 「会话记录」指开发时的一手对话过程，未落盘于 git/文档，不可独立核验；以其为唯一依据的表述视同（分析）。

## 1. 目标与范围

M2 的唯一目标是打通**工具调用闭环**：用户任务 → 模型发起 tool_calls → 本地执行 →
结果以 role=tool 回填 → 模型继续，直到不再调用工具。工具只做三个——read_file、
write_file、run_command。

明确砍掉的（都写进了设计文档边界）：

- **路径守卫、命令黑名单、diff 预览** → M3。理由是"独立切面一次性做全，避免半套
  守卫造成虚假安全感"。这个决策的价值后来被验证：M3 的 `mncc/safety/guard.py`
  一次性覆盖全部五个文件工具，而不是五处零散的 if。
- **edit_file / list_dir / grep** → M3。三个工具已足够验证闭环：读、写、执行。
- **上下文压缩、token 计量校准** → M4。M2 只在 LoopResult 里预留了 total_usage。

## 2. 开发过程

顺序是**协议层 → 工具层 → 循环层 → UI/CLI → prompt → 测试**，依赖单向向上：

1. **先定协议**（`llm/client.py`）：`ToolCall(id, name, arguments)` 加入事件流，
   `ResponseCompleted` 携带 `tool_calls` 列表。协议定型后上层才能并行开工。
2. **openai_compat 聚合**：按 `delta.tool_calls` 的 index 维护 slot，拼接
   id/name/arguments 分片。这是本期技术难度最高的一段。
3. **工具抽象**（`tools/base.py`）：Tool/ToolResult/ToolRegistry + ConfirmFn。
   execute() 集中做 JSON 解析、确认门禁、错误转译。
4. **三工具实现**（`files.py` / `command.py`）。
5. **循环合并**（`agent/loop.py`）：M1 的 `run_turn` 合并为 `run_agent_loop`——
   设计文档预留了两个选项，实现时拍板合并，REPL 与 `-p` 共用一条代码路径。
6. **UI 与 CLI**：tool_progress 单行进度、confirm_write 面板、`--yolo` 贯通、
   `-p` 模式注入硬拒回调。
7. **system prompt 升级为 §9 完整版**（先读后改/小步前进/不猜文件/诚实汇报/结束总结）。
   M2 就写全的理由：M5 评测驱动迭代打磨的就是这份文案，越早进入迭代越能积累对比数据。
8. **测试最后集中补**：新增 test_tools_base / test_tools_files / test_tools_command
   三个文件（分文件项数当时未单独记录），重写 test_loop（run_turn →
   run_agent_loop），扩充 test_openai_compat（分片聚合 5 项）与 test_cli
   （`-p` 写入拒绝、`--yolo` 放行）。

为什么这个顺序：每一层只依赖下面的层，测试写的时候被测对象已定型，避免了
接口返工导致的测试重写。（分析：从最终代码的单向依赖结构反推。）

## 3. 关键设计决策

| # | 决策 | 理由 | 被放弃的替代方案 |
|---|------|------|------------------|
| D1 | `-p` 模式 write_file 默认**硬拒**（`ConfirmRefused` 异常），提示加 `--yolo` | `-p` 常被脚本化调用，静默写文件不可接受 | 返回 is_error 让模型"换方案"——无人值守场景继续跑没有意义，必须退出码非 0 |
| D2 | tool_calls 流式分片在**客户端内聚合** | 聚合逻辑与协议强相关，loop/UI 不该懂协议碎片 | 把分片事件透传给 UI 换取"参数输入中"的中间态——M2 只需要"正在执行 xxx"，不值得 |
| D3 | 确认交互做成**回调注入**（ConfirmFn），工具本身无 IO | 工具可在无终端环境单测；REPL y/n 与 `-p` 硬拒只是两个不同的回调实现 | 工具内部直接 input()——测试要模拟 stdin，且 `-p` 无法注入策略 |
| D4 | 工具报错**不抛异常**，以 is_error 回填 role=tool | 模型必须看到错误原文才能自纠重试；抛异常终止循环就杀死了"自主恢复" | 异常上抛由用户处理——agent 可靠性的核心恰恰是模型自纠 |

实现期补充的决策（设计文档未编号，代码可查证）：

- **三级异常语义**：`ToolError`（预期内失败，is_error 回填）→ `ConfirmRefused`
  （策略硬停，穿出 execute 由 loop 捕获）→ 裸 `Exception`（兜底转 is_error，
  循环永不因工具崩溃）。见 `mncc/tools/base.py` execute() 的 except 顺序。
- **空 content 用 None 不用 ""**：`add_assistant_tool_calls` 里 content 为空时写
  None——部分兼容端点拒绝 `content="" + tool_calls` 的组合（`mncc/agent/loop.py`）。
- **中断时消息结构合法性高于一切**：KeyboardInterrupt 打断工具执行时，已完成的
  结果照常回填、未执行的补占位 tool 消息——OpenAI 协议要求每个 tool_call 都有
  对应的 role=tool 消息，否则下一轮请求被服务端拒绝。

## 4. 难点与踩坑

以下三个坑均在 M2 开发会话中真实发生（第一手记录，非推测）：

**坑 1：mock 客户端违反 stream() 契约，tool_calls 静默丢失。**
现象：`test_print_mode_write_refused_without_yolo` 断言退出码 1，实际返回 0，
stdout 还打出了第二轮回复"写完了"——意味着 ConfirmRefused 从未触发。
定位过程走了弯路：先怀疑 `registry.execute` 的 `except Exception` 吞掉了
ConfirmRefused，加了一个防御性 re-raise（不是根因但保留了下来）；再写最小
复现脚本直接调 run_agent_loop，行为正确——问题必然在 mock。最后发现
WriteToolClient 在**一次 stream() 里 yield 了两个 ResponseCompleted**，
loop 的消费循环里后者覆盖前者，tool_calls 丢失，循环直接 completed。
解决：mock 改为计数式，每次 stream() 恰好产出一个 ResponseCompleted
（这本来就是 OpenAI 协议契约，mock 违约在先）。预防：**mock 的契约要和真实
组件一致，测试 bug 的症状会伪装成被测代码 bug**——这次花了很长的定位时间，
根因却在测试自身。

**坑 2：ScriptedClient 脚本结构 typo。** scripts 类型是
`list[list[Event]]`（每个内层列表 = 一次 stream() 的事件序列），测试写成了
`list[Event]`，`yield from` 单个 ResponseCompleted 报
`'ResponseCompleted' object is not iterable`。一次修正所有测试构造。

**坑 3：错误文案与测试正则不一致。** run_command 超时文案是
"命令超过 X 秒**未结束**"，测试 `match="超时"` 不命中。改成
`match="超过.*未结束"`。教训：**断言匹配文案时优先贴近实际输出，而不是贴
自己脑中的词**。

技术难点补充（分析，非踩坑记录）：流式 tool_calls 聚合要处理分片交错、
缺省 index、id 仅首片出现等边界；实现选择了 dict[index]→slot 累积、
finish 后统一排序产出。这段逻辑后被 `INTERVIEW.md` 收录为考点 3。

## 5. 验收与数据

- **测试**：M2 结束时全量 **112 项通过，ruff 零报错**（`M2_DESIGN.md` 头部记录）。
  新增 test_tools_base / test_tools_files / test_tools_command 三个文件，
  重写 test_loop（run_turn → run_agent_loop），扩充 test_openai_compat
  （分片聚合 5 项）与 test_cli（`-p` 写入拒绝、`--yolo` 放行）。
- **验收条目对照**（`M2_DESIGN.md` §7）：验收 2/3（`-p` 写入被拒退出码非 0、
  `--yolo` 放行、工具进度显示）有等价的自动化测试覆盖；验收 1（真实 API 跑
  样例仓库修 bug）按 PROMPT.md 流程由学生在本机执行，文档未记录执行结果。
  另注（分析）：验收 1 文中"agent 自主 read → **edit** → run_command"的 edit 指的
  应是 write_file 覆盖写——edit_file 是 M3 工具，此处为设计文档的超前表述。
- **当前项目终态**：全量 306 项测试（M6 后），M2 产出的 registry/loop/compat
  测试仍在其中运行。

## 6. 一分钟面试讲述版

M2 是这个项目从"聊天机器人"变成"Agent"的一步：打通工具调用闭环。核心是三块。
第一，流式协议下 tool_calls 是按 index 交错的分片，我在协议客户端里把它聚合成
完整的 ToolCall 列表，上层循环完全不感知协议碎片。第二，工具执行的错误处理哲学：
任何失败——参数 JSON 坏了、工具名不存在、执行异常——都不抛异常，而是作为
is_error 的 role=tool 消息回填给模型，让模型看到错误原文自己纠错；只有策略性的
拒绝（比如 `-p` 无人值守模式下的写文件）才硬停任务。第三，中断语义：用户 Ctrl+C
打断工具执行时，我保证每个 tool_call 都有对应的 tool 消息，因为 OpenAI 协议要求
消息结构合法，否则后续请求直接被服务端拒。这期我印象最深的坑是 mock 客户端在
一次流里 yield 两个完成事件，tool_calls 被静默覆盖，症状像被测代码 bug，根因
却在测试——mock 的契约必须和真实组件一致。

## 7. 延伸与建议

**如果重做会改什么（分析）：**

- ToolRegistry.execute 的职责略重（解析+门禁+执行+异常转译四件事），
  M3 加守卫、M6 加 MCP 代理后可以拆出"参数解析"前置层。
- needs_confirm 是布尔，无法表达"这条命令在黑名单里要硬拒、那条只需确认"——
  M3 的命令守卫实际需要三级判定（allow/confirm/block），接口当时就该设计成枚举。
- 测试先行的顺序值得商榷：坑 1 的 mock 契约问题，若先写"契约测试"（每个
  stream() 恰好一个 ResponseCompleted）就能提前暴露。

**面试官可能的追问及答法：**

- *"为什么错误回填而不是抛异常？"* → 抛异常只有一条路：终止循环。回填给了
  模型三条路：改参数重试、换工具、诚实汇报失败。Agent 的可靠性来自自主恢复。
- *"聚合为什么放客户端不放 UI？"* → 协议细节泄漏到业务层，换协议（M6 的
  Anthropic 原生协议计划）就要改 loop/UI；放在 openai_compat 里，上层只看
  聚合后的 ToolCall。
- *"中断时为什么不直接丢掉这一轮？"* → 消息历史是跨轮共享状态，结构非法会
  污染所有后续请求；补占位消息的成本远低于重建会话。

**可选进阶方向：** 并行工具调用（OpenAI 协议支持一轮多个 tool_calls，当前
实现是串行逐个执行）；工具执行的取消传播（KeyboardInterrupt 目前只在工具
边界被捕获，长命令内部无法软取消）。
