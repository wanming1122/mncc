# 开发提示词：mncc —— 迷你 Claude Code（终端 AI 编程 Agent）v2

> 使用方法：把本提示词发给 AI 编程工具（ZCode / Claude Code / ChatGPT 等），严格按里程碑分阶段推进，每个里程碑单独开一个会话。
>
> v2 变更：新增简历目标与反向约束（§2）、`-p` 非交互模式（§4.1）、任务级 token 预算（§4.2）、上下文管理升级为两级压缩（§4.5）、新增差异化模块（§5）、新增评测体系（§6）、开源完整度要求（§10）、里程碑重构（§12）。

## 1. 角色与背景

你是一位资深的 Python 工程师，擅长 AI Agent 系统开发。我是软件工程专业大四学生，要从零构建一个"简历级"项目：一个运行在终端里的 AI 编程 Agent，功能对标迷你版 Claude Code。你的任务是带我完成设计和编码，代码质量按生产标准要求，并在每个关键决策处讲清原理。验收标准不是"能跑"，而是"能写上简历并在面试中站得住"。

## 2. 项目定位与简历目标

- 项目名：mncc（mini Claude Code），Python 包，命令 `mncc` 启动
- 一句话描述：终端里的 AI 编程助手——我用自然语言下达任务，它自主读写文件、搜索代码、执行命令、修改代码并验证，直到任务完成
- 核心认知：REPL + Function Calling 在 2026 年已是教程级标配。本项目的区分度来自三件事：**可量化的评测数据、有深度的差异化功能、开源工程完整度**
- 完成后简历上必须能写出（数字以实测为准，差异化模块按 §5 实际选择替换）：
  1. 从零实现终端 AI 编程 Agent（Python，零 Agent 框架）：手写 Agent Loop、流式 Function Calling、两级上下文压缩，支持 MCP 协议扩展工具
  2. 自建 20 任务代码修改基准与自动化评测管线，Agent 成功率从基线 __% 迭代到 __%（评测驱动的 prompt/工具描述改进，附对比数据）
  3. 工程化交付：__% 测试覆盖率、GitHub Actions CI、PyPI 可安装（`pip install mncc`）
- 反向约束：任何不能转化为上述三条之一的功能，默认砍掉

## 3. 技术栈（已定，除非我明确要求更换）

- Python 3.10+，标准库优先，第三方依赖仅限：
  - `openai`（走 OpenAI 兼容协议，可对接 GLM / DeepSeek / Qwen / 本地 Ollama）
  - `rich`（终端 Markdown 渲染、语法高亮、diff 着色）
  - `prompt_toolkit`（输入编辑与历史）
  - `pytest`（测试）
  - `mcp` 官方 SDK（若选做 §5.A；协议客户端库，不属于 Agent 框架）——手写 JSON-RPC over stdio 可加分
- 禁止使用 LangChain / LlamaIndex / 任何 Agent 框架
- 开发环境是 Windows + Git Bash：子进程统一 UTF-8 编码避免 GBK 乱码，路径处理用 pathlib

## 4. 核心功能（P0，必须全部实现）

### 4.1 交互
- `mncc` 启动 REPL：支持多行输入、历史（上箭头）、Ctrl+C 中断当前任务但不退出
- **`mncc -p "任务"` 非交互一次性执行模式**：执行完退出，退出码反映成败。这是评测管线与脚本化调用的基础，M1 就要实现
- 斜杠命令：`/help` `/clear` `/exit` `/model` `/context` `/compact`
- 模型回复流式打印 + Markdown 实时渲染；工具调用过程实时显示摘要（正在读 xxx、正在执行 xxx），不刷屏

### 4.2 Agent Loop（核心）
- 消息循环：用户任务 → 模型思考 → 发起 tool_calls → 本地执行 → 以 role=tool 消息回填结果 → 模型继续，直到模型不再调用工具（任务完成）或达到最大轮数（默认 25，可配置）
- 每轮打印 token 用量；任务级 token 预算上限（默认 200k，超限中止并标记 budget_exceeded）
- 异常（API 超时、工具报错）要有重试与友好提示，REPL 不崩溃

### 4.3 工具集（OpenAI function calling 协议，每个工具带 JSON Schema）
- `read_file(path, offset?, limit?)`：输出带行号；默认最多 2000 行，超出提示分页
- `write_file(path, content)`：写入前向用户展示内容预览并确认（--yolo 可跳过）
- `edit_file(path, old_string, new_string)`：精确字符串替换；old_string 必须唯一命中，否则报错；未命中时返回文件中最相近片段及差异提示，帮助模型自纠；修改前展示 diff（rich 渲染）
- `list_dir(path)`：树形结构，忽略 .git / __pycache__ / node_modules / venv 等
- `grep(pattern, path?, glob?)`：正则搜索，输出 文件:行号:内容，默认限 100 条
- `run_command(cmd, timeout=30)`：子进程执行，返回 stdout / stderr / exit_code；输出超长截断；危险命令需确认

### 4.4 安全
- 路径守卫：所有文件操作限制在启动目录内，解析后路径穿越（`../`、符号链接）直接拒绝
- 命令守卫：黑名单（rm -rf、mkfs、format、curl|sh、del /s 等）必拦截；其余命令首次执行需 y/n 确认；`--yolo` 全局跳过
- run_command 超时强杀进程

### 4.5 上下文管理（两级压缩——本项目第一个面试深水区）
- L1 截断：单个工具输出超限时先截断（保留首尾 + 中间省略提示）
- L2 压缩 auto-compact：总量达模型上限 80% 时，保留 system prompt 与最近 2 轮对话，其余历史发起一次额外的 summarize 调用，生成 ≤500 token 摘要注入上下文；压缩前后打印 token 对比；`/compact` 手动触发
- token 计量策略：混合启发式（中文 1 字 ≈ 1 token，英文 4 字符 ≈ 1 token），并用每次 API 响应的 usage 字段在线校准系数；能在面试中讲清误差来源

### 4.6 配置
- `~/.mncc/config.toml`：base_url、model、max_turns、token_budget、api_key（引用环境变量，不落盘明文）、mcp_servers 列表
- 示例：GLM `base_url = "https://open.bigmodel.cn/api/paas/v4/"`、DeepSeek `"https://api.deepseek.com"`
- 项目目录 `.mncc.toml` 可覆盖全局配置

## 5. 差异化模块（简历级门槛：5.A–5.C 至少选做一个，推荐 A；时间富足可多做）

### 5.A MCP 客户端（推荐，2026 生态必备词）
- stdio transport：按配置启动 MCP server，完成握手、`tools/list`，把远端工具以 `mcp__<server>__<tool>` 命名并入本地 ToolRegistry，调用时代理转发
- 验收：接一个真实第三方 MCP server（如 filesystem server）或自写 echo server，在 mncc 里成功调用其工具

### 5.B 子代理（subagent）
- 提供 `explore(task)` 工具：在独立上下文、只读工具集（read/grep/list）中执行搜索类子任务，仅把结论回传主循环
- 面试点：上下文隔离——搜索产生的海量中间输出不污染主上下文

### 5.C 检查点与回滚
- 每次 write/edit 前把原文件快照到 `.mncc/checkpoints/<任务id>/`；`/undo` 一键回滚整个任务的所有改动
- 面试点：给"agent 改坏代码"兜底的安全网设计

## 6. 评测体系（P0——本项目的核心差异，没有它就不算简历级）

- 目录结构：
  - `bench/runner.py`：拷贝 fixture 到临时目录 → `mncc -p` 执行任务 → 运行终态断言 → 记录 成败/轮数/token/耗时 到 `bench/results/*.json` → 输出汇总表
  - `bench/tasks/`：每任务一个目录，含 `init/`（初始代码）、`task.md`（自然语言任务）、`test.py`（对终态的 pytest 断言）
- 20 个任务，覆盖：bug 修复 ×6、小功能新增 ×6、重构 ×4、测试编写 ×4，分容易/中等/困难三档
- 双模式：CI 中用 mock LLM 回放固定 tool_calls 序列做回归冒烟；真实跑分在本机用真实 API 定期执行（控制任务规模控制成本）
- **评测驱动迭代（最重要的面试素材）**：首次跑分建立基线后，至少完成一轮"改 system prompt / 工具描述 / max_turns → 重跑 → 对比"，把前后数据写进 README

## 7. 进阶功能（P1，全部 P0 + 差异化模块 + 评测完成后再做）
- 会话持久化：`/save` `/resume`，JSONL 存储消息历史
- todo 工具：复杂任务让模型维护任务清单，UI 显示进度
- Anthropic 原生协议支持（第二个 Client 实现，体现抽象价值）
- git 集成：任务结束自动 git diff 并让模型写改动总结

## 8. 架构要求

单向依赖、分层清晰：

```
mncc/
  cli.py            # 入口：argparse（含 -p/--print、--yolo）、REPL 主循环
  config.py         # 配置加载（全局 → 项目级覆盖）
  llm/client.py     # LLMClient 抽象：stream(messages, tools) -> AsyncIterator[Event]
  llm/openai_compat.py
  tools/base.py     # Tool 抽象类：name、schema、run()；ToolRegistry 注册/分发
  tools/{files,search,command}.py
  agent/loop.py     # AgentLoop：消息历史 + 轮次控制 + 事件回调 + token 预算
  agent/context.py  # ContextManager：token 计量、L1 截断、L2 auto-compact
  agent/subagent.py # 若选 §5.B
  mcp/client.py     # 若选 §5.A：stdio 连接、工具发现与代理
  safety/guard.py   # 路径守卫 + 命令守卫
  ui/render.py      # rich 渲染器：流式 Markdown、diff、确认交互
  prompts/system.py # Agent 的 system prompt，独立成模块便于迭代
bench/
  runner.py
  tasks/            # 20 个任务：init/ + task.md + test.py
  results/
tests/
.github/workflows/ci.yml
```

动手前先给出每个模块的类与方法签名设计，说明为什么这样分层，经我确认后再写实现。

## 9. Agent 内置 System Prompt（产品核心，认真写）

写在 `mncc/prompts/system.py`，要求模型遵守：
- 先读后改：edit 之前必须先 read_file 看过目标文件
- 小步前进：一次只做一件事，改完立即用 run_command 验证（能跑测试就跑测试）
- 不猜文件：没读过的内容不要假设
- 诚实汇报：验证失败要说失败，不许粉饰
- 结束时输出：改了什么、为什么、如何验证的

随代码交付这份 prompt 的完整文案，并解释每条规则对应解决什么实际问题。评测驱动的迭代（§6）主要就是打磨这份 prompt 和各工具的描述文案。

## 10. 工程质量与开源完整度
- pytest 单元测试：edit_file 唯一性校验、路径守卫（穿越用例）、命令守卫（黑名单）、ContextManager 两级压缩、工具 schema 合法性；LLM 一律 mock
- 集成测试（cassette 模式）：mock LLM 回放脚本化 tool_calls 序列，覆盖验收场景 1/4 的无 API 版本
- 全量 type hints；ruff 检查通过
- CI：GitHub Actions，跑 ruff + pytest（含 bench 冒烟），README 挂 badge
- 发布：PyPI（uv build，打 tag 触发发布；退而求至少 GitHub Release + `pipx install git+URL`），README 顶部必须有一行安装命令
- LICENSE（MIT）
- README.md：安装、配置示例、功能演示 GIF、架构图（mermaid）、**跑分表（含迭代前后对比）**、"为什么不用框架"等设计决策、局限与未来工作
- INTERVIEW.md 面试考点清单及答案要点：Agent Loop 状态机、tool_calls 消息结构、流式下如何聚合工具调用增量片段、两级压缩的信息损失权衡、token 估算误差来源、MCP 握手流程、子代理的上下文隔离收益、评测方法论（成功率定义、防 flaky、样本量局限）、"什么场景该用框架"、`-p` 模式与 REPL 的架构分叉

## 11. 验收场景（完成后的自测标准）
1. 样例仓库（3~4 个 Python 文件的计算器，含 2 个 bug 和对应 pytest）中输入："测试挂了，找到 bug 修复并让全部测试通过" → agent 自主 read → grep → edit_file → run_command(pytest) → 汇报结果
2. 输入："在 ./demo 新建一个命令行 TODO 应用（增/删/查/完成）" → 多轮工具调用后程序可运行
3. 读取 5000 行大文件：正常分页，上下文不爆（必要时触发 auto-compact）
4. 输入危险指令（如"删掉 D:\ 下所有文件"）：被守卫拦截并要求确认
5. 断网 / API key 错误：REPL 不崩，给出可读错误
6. `mncc -p` 在 bench 单任务上端到端跑通，退出码正确
7. 差异化模块各自验收通过（见 §5）
8. README 出现跑分表，且至少一组评测驱动迭代的前后对比

## 12. 开发节奏（严格按里程碑，每步可运行；总计约 4–6 周业余时间）
- M1（2–3 天）：REPL + 流式对话 + `-p` 非交互模式（无工具）
- M2（3–5 天）：工具调用闭环跑通（只做 read_file / write_file / run_command 三个）
- M3（3–5 天）：补全工具集 + diff 预览确认 + 路径/命令守卫
- M4（3–5 天）：两级上下文压缩 + token 计量 + 配置系统
- M5（约 1 周）：bench 20 个任务 + 首次跑分 + 一轮评测驱动迭代
- M6（1–2 周）：差异化模块 + CI + 发布 + README / GIF / INTERVIEW.md

每个里程碑：先 1 段设计说明 → 编码 → 我在本机跑通验收 → 进入下一个。禁止一次生成全部代码。

## 13. 沟通规则
- 有歧义先问我，不要替我做主
- 每个关键决策附"为什么"，控制在 5 句以内
- 代码注释解释"为什么"，不解释"是什么"
