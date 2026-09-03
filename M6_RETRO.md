# M6 复盘：手写 MCP 客户端 + CI + 发布 + INTERVIEW.md + README 补全

> 写作日期：2026-09-03。事实来源：`M6_DESIGN.md`（D1–D8）、git 提交链
> `3d3b42e → 59dd33e → e61248b → 0dbf636 → e6f67a6 → a6ef6bd → b1eed9c → f500dc9`、
> `INTERVIEW.md`、`README.md`、issue #1 的 CI traceback。M6 是唯一一个"设计、编码、
> 验收、CI 排障在同一工作会话内完成"的里程碑，过程证据有 git 哈希可查；
> 开发会话内发生的事实标注「（对话记录）」，推测标注「（分析）」。

## 1. 目标与范围

**做什么**：PROMPT §5.A 差异化模块（MCP 客户端）+ §10 开源完整度（GitHub Actions
CI、发布、INTERVIEW.md、README 补全）。

**明确不做什么**（砍掉的及原因）：
- §5.B 子代理 / §5.C 检查点回滚——三个差异化模块至少选一，选了推荐且 2026 生态
  必备的 5.A，不贪多；
- **不引入 `mcp` 官方 SDK**——PROMPT 明示"手写 JSON-RPC over stdio 可加分"，
  且依赖白名单（openai/rich/prompt_toolkit/pytest/ruff）没有它；
- 不新增 CLI 旗标——MCP 走配置驱动，与 M5 D3 的克制一致；
- 不碰 `loop/safety/llm/ui/tools/base.py`——MCP 工具复用既有 Tool 抽象。

## 2. 开发过程

顺序是"先定契约、再写协议层、最后接主流程"，每步跑测试与 ruff 再继续：

1. **设计确认**：先向用户复述 D1–D8，确认后才编码（设计文档明文禁止跳过确认）。
2. **协议层**（`mncc/mcp/protocol.py`，~112 行）：帧编解码纯函数，先写完就配
   framing 单测（往返/跨 chunk/非法帧/EOF）。
3. **客户端层**（`mncc/mcp/client.py`，~283 行）：`McpClient`（子进程生命周期 +
   reader 线程 + 按 id 关联响应）→ `McpTool(Tool)` → `attach_mcp_tools`。
4. **验收工具**（`mncc/mcp/echo_server.py`）：自写最小 server，让端到端测试和
   用户验收都不依赖 Node/npx。
5. **配置与装配**：`config.py` 扩展数组内联表 + `mcp_servers` 校验；
   `cli.py` 加 `_connect_mcp` 与 finally close。
6. **测试**：4 个新测试文件（protocol/client/echo/tool）+ `test_cli.py`/`test_config.py`
   补测，共 50 项（32 MCP + 15 config + 3 cli，`pytest --collect-only` 实测），三类策略见 D8。
7. **验收**：本机 `-p` 与 REPL 实测调通 `mcp__echo__echo` 后才 push 触发 CI。

为什么这个顺序：framing 是最底层、可独立验证的纯函数，先站稳它，上面的
client/echo server 调试时就能排除协议层嫌疑；echo server 早于真实 server 接入，
让"验收不依赖外部环境"成为测试策略的一部分。

## 3. 关键设计决策（面试追问高发区）

| 决策 | 内容与理由 | 被放弃的替代方案 |
|---|---|---|
| **D1 手写协议** | 只实现 2024-11-05 最小子集（initialize/initialized/tools/list/tools/call/shutdown）。零框架定位的一致延伸，framing 是面试深水区 | `mcp` SDK：接口隔离已做好（`McpClient` 屏蔽传输层），未来可换 |
| **D2 扩展 TOML 子集** | 数组元素支持内联表即可满足需求（~90 行解析器）；name 限 `[a-z0-9_-]+` 防命名空间注入 | 引入 tomli：白名单外；只差一种语法不值得加依赖 |
| **D3 `mcp__` 前缀 + 默认确认** | 复用同一 ToolRegistry 自动获得 function calling/确认门禁/错误回填；远端副作用是黑盒，`needs_confirm` 默认 True | 独立工具体系：违背"MCP 不是另一套体系"；默认免确认：安全隐患 |
| **D4 finally 关闭** | REPL 与 -p 统一出口 close，连接失败的 server 跳过不拖垮主流程 | `atexit`：-p 一次性进程不该把清理交给 GC |
| **D5 同步阻塞 + 30s 超时** | 与 agent 串行一问一答模型一致；超时抛 `McpError` → is_error 回填，模型可见可自纠 | 并发请求：当前无需求，复杂度不划算 |
| **D6 CI 只跑 mock** | ruff + pytest + `bench/runner.py --mode mock`，零真实 API | 真实跑分进 CI：无 key、成本不可控 |
| **D7 发布标准** | GitHub Release + `pipx install git+URL`（console script 已就绪，零额外工作） | PyPI：需账号 token，后置可选 |
| **D8 测试三类** | framing 纯函数 / stub 子进程回放（`python -c` 脚本，init/call 模式可编程）/ echo server 端到端 | 只测纯函数：漏掉生命周期；依赖真实 server：CI 不可复现 |

共性：D1/D2/D8 是同一主题——**依赖白名单内自建、小步可验证**；D3/D4/D5 是另一主题
——**远端能力接入必须带上本地的安全与生命周期语义**，不能因为是"外部工具"就降级处理
（`tools/base.py` 的抽象没为 MCP 改一行，`cli.py` 只加了一个 `_connect_mcp` 装配点）。

## 4. 难点与踩坑

**坑 1：framing 的 `_readline` 未剥行尾 `\r\n`**（对话记录，修复后测试全绿）
- 现象：decode 一批测试失败，报"对端在帧头部中途关闭了连接"。
- 定位：空行判定用 `line in (b"", b"\r")`，但读到的是 `b"\r\n"`——三个条件全不中，
  头部永不终止。
- 解决：`_readline` 统一剥掉 `\r\n`，空行判定简化为 `not line`。
- 预防：协议解析函数先写"字节级往返"单测再接上层；编码/解码共用同一种行尾约定。

**坑 2：重构时误删 `_HEADER_END`、漏定义 `McpError`**（对话记录）
- 现象：ruff 直接报 F821，`ImportError: cannot import name 'McpError'`。
- 解决：补回常量与类定义。教训：每完成一个单元立即 `ruff check` 是设计文档
  写死的开发协议，这次它当场抓住了问题，没让它流到测试阶段。

**坑 3：CI 首跑失败，但匿名无法读日志**（提交 `59dd33e`/`e61248b`）
- 现象：run #1 pytest 步骤失败（23s 内），logs API 返回 403 "Must have admin
  rights"，匿名网页也看不到日志。
- 定位过程（这才是重点）：加诊断步骤——pytest `continue-on-error`，失败时重跑
  并把完整输出 **自动创建成公开 issue**（`59dd33e`）；期间还踩了 workflow 块标量
  里写 ` ``` ` 导致 "Invalid workflow file: yaml syntax on line 35"、整个 run
  0 jobs 直接失败（`0dbf636` 修复缩进）。最终 issue #1 拿到完整 traceback。
- 根因（issue #1 实锤，非推测）：两个**既有测试的跨平台缺陷**，此前 254 项只在
  Windows 本机跑过——
  ① `test_timeout_shows_partial_output`：`subprocess.run` 在 POSIX 超时后不收集
  部分输出，`已产生` 提示缺失；
  ② `test_list_dir_symlink_dir_not_followed`：Windows 建不了软链被 skip，Linux
  真跑后断言 `secret.py not in out` 被同目录**真实文件夹** `real/` 下的同名文件误伤。
- 解决（`e6f67a6`）：① `tools/command.py` 超时改为**读取线程泵取 + `wait(timeout)`**
  ——实测确认 Windows 上 `communicate(timeout)` 的 `exc.stdout` 也是 `None`，两个
  标准库方案都不可靠，泵取是跨平台保留部分输出的唯一稳妥解；② 断言改精确为
  `"link/secret.py" not in out`，另把超时测试的 `print` 加 `flush=True`（管道块
  缓冲 1s 内根本到不了读端）。
- 今后如何预防：跨平台项目**第一天就该有 CI**——M6 之前所有测试只验证过 Windows；
  "skip 掉的平台分支"等于零覆盖，断言要写成不因平台差异而语义漂移的形式。
  这与 M3 的 `true` 命令坑（M3_RETRO：Windows shell 无 `true`）是同一主题的
  第二次爆发——M3 复盘已预言"M6 e6f67a6 是同族问题"，果然。

**坑 4：慢测试**（附带收益，实测数据）
- `command.py` 旧实现下两个超时测试各 60s（Windows 等孤儿进程），全量 128s；
  泵取方案后全量降到 **34.57s**。修正确性顺带修了开发体验。

## 5. 验收与数据

- 测试：passed **254 → 304**（collected 256 → 306，即 304 passed + 2 skipped），
  新增 50 项，零真实网络依赖；ruff 零报错。
- 主提交 `3d3b42e`：16 文件，+1709/-25。
- 本机验收：`mncc -p "调用 mcp__echo__echo …" --yolo` 退出码 0 且回复含回显；
  REPL 同工具调通；`~/.mncc/config.toml` 追加 echo server 配置。
- CI：最终 workflow（`a6ef6bd` 恢复简洁版）runs `e6f67a6`、`a6ef6bd` 连续 **success**，
  README badge 亮。
- 安装：本地 `pip install -e .` 后 `mncc --version` / `python -m mncc` 验证通过；
  `pipx install git+URL` 为 D7 发布标准（PyPI 后置可选）。
- 收尾：`b1eed9c`（--help 文案从 M2 更新为项目全貌）、`f500dc9`（ruff format
  全量 30 文件纯格式化）。

## 6. 一分钟面试讲述版

M6 我做的是 MCP 客户端，关键是**手写**——不引官方 SDK，只实现 2024-11-05
最小子集：initialize 握手、tools/list、tools/call、shutdown，协议加客户端
四百来行，加自写的 echo 验收 server 共五百行。
framing 用 Content-Length 头，重点是长度必须按 UTF-8 字节数算，解码要跨 read
分块重组。架构上最满意的一点是 MCP 不是另一套体系：远端工具包一层 McpTool
挂进现有 ToolRegistry，就自动获得 function calling、确认门禁和错误回填，
主循环零改动。远端副作用是黑盒，所以确认默认开启。生命周期用 finally 统一
关闭，shutdown 带 5 秒超时再 terminate，防僵尸子进程。CI 是这期另一收获：
首跑就失败，而 Linux 暴露了两个只在 Windows 测过的跨平台缺陷——subprocess
超时在 POSIX 不收集部分输出，我改成读取线程泵取才跨平台可靠。这件事让我
理解了"测试绿"只等于"在跑过的平台上绿"。

## 7. 延伸与建议

**如果重做会改什么**（分析）：
- CI 应该在 M1 就建——M6 的跨平台修复本质是在还 M1–M5 的债，越早越便宜；
- `McpClient` 的请求/响应按 id 关联目前忽略无关消息会一直空转，可加"未匹配
  消息回灌队列"的显式测试（分析，当前实现依赖 server 不乱发）；
- 内联表解析器的 `_split_top_level` 手写字符状态机，值得补 property-based
  测试（hypothesis）对抗畸形输入。

**面试官可能的追问及答法**：
- "为什么不用官方 SDK？"——零框架是项目定位，且协议子集小到手写成本可控
  （framing ~30 行）；接口隔离保证了未来可替换。关键是讲清**权衡**：代价是
  协议演进要自己跟。
- "并发请求怎么办？"——当前同步阻塞与 agent 串行模型一致；要并发的话按
  id 分发的 reader 线程架构天然支持，只需把 `_recv` 改成 future map。
- "为什么要 pump 线程而不等进程结束再读？"——输出管道满会死锁子进程；
  且超时场景必须边跑边收才有"部分输出"可回显。

**进阶方向**：接官方 filesystem server（需 Node，验收 4 的可选条目）；
MCP resources/prompts 能力；把 CI 的 issue 诊断通道改成正式的 workflow artifact。

---

## 项目总览表（M1–M6）

| 里程碑 | 核心产出 | 测试数增量 | 最大难点 | 一句话收获 |
|---|---|---|---|---|
| M1 | REPL + 流式对话 + `-p` 非交互模式（无工具）；配置/LLM 抽象/UI/退出码契约 | 0 → 47（会话记录） | openai 3.x 弃 httpx 改 httpx2；prompt_toolkit 在 Git Bash 构造即崩 | 先让"壳"立起来，`-p` 的 IO 与退出码契约是评测管线地基 |
| M2 | 工具调用闭环（read_file/write_file/run_command）+ ToolRegistry + 流式 tool_calls 分片聚合 | 47 → 112（+65） | mock 客户端一次 stream() 里 yield 两个完成事件，tool_calls 静默丢失 | 错误回填而非抛异常——Agent 可靠性来自模型自纠 |
| M3 | 工具补全（edit_file/list_dir/grep）+ diff 预览 + 路径/命令守卫 | 112 → 200（+88，含 2 skip） | 守卫顺序曾把已授权判断放黑名单前，批准过的 `rm -rf` 反被放行 | 安全是横切面，一次性做全；`--yolo` 只跳过确认、不越过红线 |
| M4 | 两级压缩（L1 截断 / L2 auto-compact）+ token 在线校准 + 配置补全 | 200 → 232（+32） | 压缩降级路径与"原子轮"消息结构合法性 | 压缩不可逆，先拍死降级与容错边界再实现 |
| M5 | bench 20 任务评测管线 + 基线 20/20 + 一轮评测驱动迭代（无效已回滚） | 232 → 254+2（+22） | 服务端中途断供致 17 任务秒败难归因；"幻觉完成"负样本实证 | 判分以终态 pytest 为准、数据说话、单跑不重试 |
| M6 | 手写 MCP 客户端（JSON-RPC over stdio，零 SDK）+ CI + 发布 + INTERVIEW/README | 254+2 → 304+2（+50） | Linux CI 暴露的两个既有跨平台缺陷（subprocess 部分输出、软链断言） | 手写协议并复用本地抽象链；"测试绿"只等于"在跑过的平台上绿" |
