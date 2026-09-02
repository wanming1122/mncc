# INTERVIEW.md — 面试考点清单

> 本项目的"简历级"卖点：**零 Agent 框架**，从零手写 Agent Loop / 流式 Function
> Calling / 两级压缩 / MCP 客户端，并自建评测体系。以下按考点给出"答案骨架"，
> 面试时据此展开；每节末尾的「工程取舍」是追问高发区。

## 1. Agent Loop 状态机与退出码契约

- 终态枚举：`completed` / `max_turns` / `budget_exceeded` / `interrupted` / `error`。
  对应退出码：0（completed）/ 1（其余失败）/ 130（SIGINT 惯例）。
- 状态机驱动评测管线：bench 靠退出码判分，`--stats-json` 把 轮数/token/耗时/状态
  落盘成机器契约（所有 graceful 终态都记账，唯一例外是外部强杀 timeout——进程没机会写）。
- 工程取舍：为什么用退出码而非输出文本判分？——文本不可靠（模型自报"成功"可能是幻觉），
  退出码 + 客观测试是确定性信号。

## 2. tool_calls 消息结构（OpenAI 协议）

- 模型回复 `tool_calls: [{id, function:{name, arguments}}]`；执行后以
  `role="tool"`、`tool_call_id` 关联回填，再送回模型完成多轮工具循环。
- 关键陷阱：`role="tool"` 必须带对应 `tool_call_id`，且顺序/结构非法会导致
  OpenAI 兼容服务端报 400——项目里有一套"中断消息结构保护"专门防这个。
- 工程取舍：工具执行失败（is_error）也回填原文而不是抛异常——模型看到错误原文才能自纠重试，
  这是 agent 可靠性的核心，不是把错误吞掉。

## 3. 流式工具调用分片聚合

- 服务端把一次 tool_call 的 `name`/`arguments` 切成多个 delta 增量下发；
  客户端需要**按 index 聚合**（同 index 的增量拼一起），等 `finish_reason` 落定再整体处理。
- 工程取舍：客户端聚合 vs 业务层感知——聚合放在协议客户端（openai_compat）里，
  业务层（loop）只看到完整工具调用，两层职责单一，也方便换协议。

## 4. 两级压缩的信息损失权衡

- L1 截断：丢弃最旧的 assistant 细节；L2 摘要：把旧历史压成一段摘要（专门摘要 prompt）。
- 权衡本质：保留"当前任务上下文" vs 腾出"窗口空间"；截断丢细节、摘要丢精确度，
  摘要生成本身也耗 token。阈值可配（默认窗口 80% 触发）。
- 工程取舍：auto 与手动共用同一实现（/compact 手动触发 = 阈值拉低到 0 的 auto）。

## 5. token 估算误差与在线校准

- 字符→token 的经验系数有误差（中文/代码差异大）；方案是**在线校准**：
  每次真实 API 用量回填后反推实际系数，逐步逼近，误差收敛。

## 6. MCP 握手流程 + framing（M6 核心）

- framing：`Content-Length: N\r\n\r\n<body>`。为什么长度头而不是行分隔？——
  JSON 正文本身可含 `\r\n`，行分隔不可靠；N 必须按**字节数**（UTF-8 中文 3 字节）。
- 解码要跨 `read()` 分块重组——body 可能被拆到多次读里，必须"读满 N 字节"。
- 握手序列：`initialize`（客户端提议协议版本 + capabilities，服务端回协商结果）
  → `notifications/initialized`（通知，无响应）→ 之后才可 `tools/list` / `tools/call`
  → 收尾 `shutdown`。
- 工程取舍：为什么手写而不引 `mcp` SDK？——项目定位"零框架手写"，MCP 手写与之一致；
  用 SDK 就把 framing/握手这个深水区外包了。代价是协议演进需自己跟进；用接口隔离，
  未来换 SDK 不动上层。

## 7. 评测方法论

- 成功率定义：**客观断言**（任务自带 pytest，终态全绿）为主，模型自报为辅——
  "幻觉完成"（自报成功、断言挂）是真实抓到的负样本。
- 防 flaky：确定性任务 + 单跑不重试（服务端断供导致整轮作废也不重刷分，
  诚实数据纪律优先于好看数字）。
- 样本量局限：20 任务是"趋势"不是"显著性"；对比时看方向和量级，不看小数点。
- 成本/规模权衡：mock 冒烟（cassette 回放）守管线不腐坏，真实跑分只在本机按需跑。
- `--stats-json`：机器契约设计——状态/轮数/token/耗时/回复长度，一次性落盘，评测可复现。

## 8. 框架 vs 手写（零框架的理由）

- 教学/简历定位：手写才能讲清 Agent Loop 的每个状态转移；用框架 = 把核心竞争力外包。
- MCP 同理：协议是公开规范（JSON-RPC over stdio），手写成本可控（framing ~30 行、
  四个方法 ~100 行），换来的是对协议的深度理解。
- 代价要诚实：没有框架的并发/容错/协议演进支持，靠自己的抽象层隔离。

## 9. `-p` 与 REPL 的架构分叉

- 共用同一套：load_config / Session / run_agent_loop / 同一工具注册表。
- 只差三件事：输入来源（argv vs 交互输入）、确认策略（ConfirmRefused 硬拒 vs 交互 y/n）、
  输出目的地（stdout 纯净 vs 混排）。单测只测 -p 与可注入的纯函数，REPL 靠手动验收。
- 工程取舍：把"确认"注入为回调（ConfirmFn），工具/loop 不碰终端——
  -p 与 REPL 只是两种不同的确认实现，UI 换法不动核心。

## 10. 用数据说"不"：一轮无效迭代的完整闭环（M5 范例）

- 假设：system prompt 加纪律 7「做完即止」→ 少做无关改动。
- 实验：基线 100% → 迭代 95%（一个任务 22 轮 99.5k tokens，"幻觉完成"被客观断言抓住）。
- 决策：无收益且更贵 → 回滚；对比表与原始数据全部保留，不美化。
- 要点：**任何迭代改动都以 README 对比数据说话**；负样本和正样本一样是证据。

---

## 附：项目演进与代码位置速查

| 考点 | 代码 | 关键行 |
|---|---|---|
| Agent Loop | `mncc/agent/loop.py` | `run_agent_loop` |
| 压缩 | `mncc/agent/context.py` | L1/L2、校准 |
| 工具门禁 | `mncc/tools/base.py` | `ToolRegistry.execute` |
| MCP framing | `mncc/mcp/protocol.py` | `encode_message` / `decode_message` |
| MCP 生命周期 | `mncc/mcp/client.py` | `McpClient.connect/close`、`attach_mcp_tools` |
| 评测 | `bench/runner.py`、`bench/report.py` | 判分与报告 |
| 配置 | `mncc/config.py` | `parse_toml_subset`、`_validate_mcp_servers` |
