# M1 复盘：REPL 流式对话与 `-p` 非交互模式——先让"壳"立起来

> **依据说明**：M1 没有设计文档（`M*_DESIGN.md` 惯例自 M2 才建立），且 M1–M5 被压缩为
> 单提交 `2e305aa`，过程无法从 git 还原。本文依据 PROMPT.md §12 的 M1 定义、M1 开发
> 会话的一手过程记录（下称"会话记录"）、现存代码与回归测试反推；标注（分析）的为推断。

## 1. 目标与范围

**目标**（§12）：REPL + 流式对话 + `-p` 非交互模式，无工具，预估 2–3 天。

**做了**：可安装包骨架；配置子系统（默认值 → 全局 → 项目 → 环境变量 → 命令行五级合并，
`mncc/config.py`）；LLM 抽象 + OpenAI 兼容流式客户端（`mncc/llm/`）；`Session`/`run_turn`
（`mncc/agent/loop.py`）；token 估算启发式（`mncc/agent/context.py`）；rich 流式渲染
（`mncc/ui/render.py`）；REPL 与 `-p`（退出码 0/1/130）。

**明确不做**：工具集（M2）、守卫（M3）、压缩与预算执行（M4）——`max_turns`/`token_budget`
字段已存在但不消费；`/compact` 是指向 M4 的占位（M4 实现为真压缩，
有 `tests/test_cli.py:100` 的手动触发测试作证）。

**范围外决策**：§12 把"配置系统"排在 M4，M1 提前做了核心子集——会话记录的理由是
"可运行的 CLI 第一天就需要 base_url/model/key，没有配置无法验收"。（分析）这符合
"每步可运行"的节奏，M4 只需增量补压缩相关配置。

## 2. 开发过程

实际一个会话完成，顺序：环境检查 → 包骨架 → 自底向上编码 → 集中补测试 → 测试修错 →
lint → 冒烟矩阵 → 真实 API 验收。

1. **环境检查先行**：Python 3.12.2；发现本机已有 `MIMO_API_KEY`（OpenAI 兼容端点，
   模型 `mimo-v2.5`），成为全程的真实验收通道。
2. **自底向上**：config → llm 抽象 → openai_compat → agent → ui → cli，依赖方向与
   §8 单向分层一致，mock 边界清晰。
3. **测试后置**（M1 阶段做法，与后续里程碑最大差异）：模块成型后集中写 6 文件 47 用例，
   LLM 全部脚本化假对象回放（§10 红线）。首轮 pytest 45 中 5 失败（见 §4）。
4. **冒烟矩阵**：`--version`/无 key/服务不可达/真实 API/REPL 管道——抓到两个单测
   没覆盖的真 bug（坑 3/4）。
5. **收尾固化**：配置写入 `~/.mncc/config.toml`（`api_key_env` 指名探测链外的变量名），
   key 用 `setx` 持久化、不落盘。

（分析）"测试后置"的教训直接促成 M2 起设计文档内置"测试计划"章节、测试与编码交替。

## 3. 关键设计决策

| # | 决策 | 理由 | 被放弃方案 |
|---|---|---|---|
| D1 | 手写 TOML 子集解析器（`parse_toml_subset`，M1 时 ~70 行，M6 扩展内联表后 ~90 行，带行号报错） | 依赖白名单不含 tomli；`tomllib` 需 3.11 而承诺 3.10+；配置只需 str/int/bool/数组 | tomllib / tomli |
| D2 | `stream()` 同步 `Iterator[Event]` 而非 §8 草案 AsyncIterator | 执行严格串行无并发收益；Ctrl+C 打断生成中回复在同步阻塞下天然成立；asyncio 取消 × prompt_toolkit 在 Windows 坑多（`mncc/llm/client.py` docstring） | asyncio（M6 手写同步 MCP 客户端佐证此选型） |
| D3 | 事件流（`TextDelta`/`ResponseCompleted`）+ 四级异常分层 | 上层不碰 SDK 类型；换协议与 mock 只动实现层 | 上层直接用 SDK 对象 |
| D4 | `-p` IO 契约：stdout 只有回复，用量/错误走 stderr；退出码 0/1/130（`EXIT_*`） | 评测管线靠退出码判分、脚本消费 stdout 不被污染（INTERVIEW.md §1 提炼为考点） | 输出文本判分 |
| D5 | api_key 不落盘：配置只写 `api_key_env`，按 `MNCC→OPENAI→ZHIPUAI→GLM` 探测 | §4.6 安全要求；报错列出尝试过的变量名 | key 明文入配置 |
| D6 | 多行输入 Esc+Enter / Ctrl+J，非 `multiline=True` | 后者把 Enter 变换行，破坏单行肌肉记忆（`build_prompt_session` docstring） | `multiline=True` |

## 4. 难点与踩坑

**坑 1：openai 3.x 弃 httpx 改 httpx2。** 现象：测试 `import httpx` 失败（装的是
openai 3.6.0）。定位：`inspect.getsource(openai._exceptions)` 发现异常全基于 `httpx2`。
解决：探测式导入 `try: import httpx2 as httpx`（`tests/test_openai_compat.py:11-14`，
至今仍在）。预防：不硬依赖快速演进库的传递依赖。

**坑 2：配置校验把缺省键当非法。** 现象：`test_defaults` 报 `max_turns 必须是正整数`，
但根本没配置它。定位：`raw.get(key)` 返回 `None`，"未出现"被误判为"类型错误"。
解决：`if key in raw` 再校验。预防：校验须区分"缺省"与"显式非法"，全默认边界测试必须有。

**坑 3：错误路径重复渲染 + 误标"已中断"。** 现象：服务不可达冒烟时错误面板打印两次，
且错误路径显示"⚠ 已中断"。定位：`run_turn` 与 `run_print_mode` 各渲染一次；错误与中断
共用 `stream_abort`，文案写死。解决：`stream_abort` 加 `note=None`（`mncc/ui/render.py:88`）；
`Renderer` 增 `error_console` 注入位，`-p` 错误走 stderr。回归测试
`test_main_print_mode_llm_error_once_on_stderr`（`tests/test_cli.py:222`）断言错误恰好
一次且不在 stdout。预防：IO 职责第一版就定"谁渲染、到哪个流、几次"；中断与错误不许
共享出口文案。

**坑 4：prompt_toolkit 在 Git Bash(mintty) 构造即崩。** 现象：REPL 冒烟直接 traceback
`NoConsoleScreenBufferError: Found xterm-256color, while expecting a Windows console`——
而 §3 声明的开发环境正是 Windows + Git Bash。定位：Win32 输出后端拿不到控制台屏幕
缓冲区。解决：构造异常时降级 `input()` 的 `_FallbackPrompt`（`mncc/cli.py:133-165`）；
第一版还因 `prompt(bottom_toolbar=...)` 签名不匹配崩过一次，最终 `**_kwargs` 吞掉
增强参数。回归测试 `test_build_prompt_session_falls_back`（`tests/test_cli.py:331`）。
预防：环境差异要进冒烟矩阵。M6 的 Linux CI 以另一种形式再次应验（`e6f67a6`：
subprocess 超时收集部分输出的 Windows/POSIX 差异，改用泵线程）——不是终端问题，
但同属"只在非开发环境显形"的平台差异。

**坑 5（小）**：重写 `render.py` 丢失 `print_text`，`/help` 测试当场抓住。预防：整文件
重写后先跑全量测试。

## 5. 验收与数据

- M1 结束时 47 个单元测试全绿（会话记录；config/context/openai_compat/loop/cli/conftest
  六文件）、ruff 零告警。当前主干 306 个（15 文件，2026-09-03 实测），M1 回归测试全部存活。
- 冒烟矩阵（会话记录）：`--version` 0；无 key → 可读指引 + 1；服务不可达 → 单次
  错误面板 + 1（坑 3 修复后复测）；真实 API 流式回复 + usage（138/253）+ 0；REPL
  管道对话 + `/exit` 干净退出。2026-09-03 于当前 HEAD 复测全部复现（无 key 需空值
  覆盖模拟——沙箱 `env -u` 会静默吞掉子进程，本身是个复测方法坑）。
- 对应 §11 场景 #5、#6 的无工具版本；实际耗时一个会话，快于预估 2–3 天。

## 6. 一分钟面试讲述版

> M1 我做的是迷你 Claude Code 的"壳"：REPL 加非交互的 `-p` 模式，没有工具。三个有
> 含金量的点。第一，`-p` 的 IO 契约——stdout 只输出模型回复，用量和错误走 stderr，
> 退出码 0/1/130。这是评测管线的地基：bench 靠退出码判分，脚本消费 stdout 不被污染。
> 第二，我放弃了 asyncio，LLM 客户端用同步迭代器产出事件流，因为 agent 执行模型严格
> 串行，asyncio 没有并发收益，反而让 Ctrl+C 打断生成中的回复变得很难写——同步阻塞下
> 它天然成立。第三是两个真实踩坑：openai 3.x 把 HTTP 层从 httpx 换成 httpx2，测试做
> 探测式兼容；prompt_toolkit 在 Git Bash 下拿不到 Win32 控制台、构造即崩，而那正是
> 我的开发环境，我加了 input() 降级路径并用回归测试锁死。M1 结束 47 个测试全绿，
> 真实 API 端到端跑通。

## 7. 延伸与建议

**如果重做**：① 设计文档惯例应从 M1 建立——M1 设计只存在于会话里，复盘只能反推；
② "渲染几次、到哪个流"应在第一版写成约定（坑 3 根因）；③ CI 若 M1 就在，
prompt_toolkit 终端兼容会更早暴露。

**追问预演**：*"为什么不用 asyncio？"*——答 D2，补 M6 同步 MCP 互为佐证，抽象层保证
未来改动收敛。*"stdout/stderr 为何分离？"*——答 D4，接 M5 的 `--stats-json` 机器契约
（INTERVIEW.md §1/§7 同一脉络）。*"token 为何中文 1 字英文 4 字符？"*——BPE 对中文
约一字一刀、英文常见词合并激进；M4 用真实 usage 在线校准（INTERVIEW.md §5）。

**进阶方向**：REPL 优雅关闭（SIGTERM 语义）；`-p` 输出模式开关（如 `--no-stream`）；
会话持久化（§7 P1）。
