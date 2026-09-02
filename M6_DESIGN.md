# M6 设计文档：MCP 客户端（§5.A）+ CI + 发布 + INTERVIEW.md + README 补全

> 状态：**已完成（2026-09-02 实现并验收）**。实现照本设计执行：
> - 新增 `mncc/mcp/`（protocol.py / client.py / echo_server.py），手写 JSON-RPC over stdio，零 SDK（D1）；
> - `config.py` 扩展数组内联表 + `Config.mcp_servers` 校验（D2）；`cli.py` `_connect_mcp` + main() finally close（D4/D5）；
> - 新增测试 **50 项**（D8 三类：framing 纯函数 / stub 子进程回放 / echo server 端到端）；
>   全量 **304 passed + 2 skipped**、ruff 零报错；
> - 本机验收通过：`-p` 与 REPL 均成功调用 `mcp__echo__echo` 并含回显（已向 `~/.mncc/config.toml` 追加 echo server）；
> - CI（`.github/workflows/ci.yml`）、`INTERVIEW.md`、README 补全、路线图 M6 勾选均已就位；
> - **CI 首跑暴露两个既有跨平台缺陷并已修复**（此前 254 项只在本机 Windows 验证过）：
>   ① `tools/command.py` 超时改用读取线程泵取 + wait，跨平台保留"已产生的输出"回显
>   （`subprocess.run` 在 POSIX 不收集、Windows `communicate` 超时也不带输出）；
>   ② `test_tools_search.py` 软链用例断言改精确（`link/secret.py` not in out），
>   原 `secret.py` not in out 在 Linux 上会因真实目录 `real/` 误报；
>   CI 现全绿。
> - 发布按 D7：GitHub Release + `pipx install git+...`（PyPI 后置可选）。
>
> 范围：PROMPT.md §5.A（MCP 客户端，用户已选定）+ §10（GitHub Actions CI、发布、
> INTERVIEW.md、README 补全）。不包含 §5.B/§5.C（子代理/检查点回滚，均未选）。
>
> 前置事实：git 仓库已初始化并推送 GitHub（`wanming1122/mncc`，首次提交 2e305aa）。

## 1. 现状盘点（新对话开发者必读）

M5 已收官（基线 20/20、254 测试全绿、ruff 零报错）。M6 复用的既有基建与唯一要改动的核心：

| 位置 | 现状 | M6 改动 |
|---|---|---|
| `mncc/mcp/` | 不存在 | 新增包：protocol.py（帧编解码）、client.py（McpClient/McpTool/attach）、echo_server.py（自写验收 server） |
| `mncc/config.py` | TOML 子集解析器只支持 字符串/整数/小数/布尔/字符串数组；M5_DESIGN 已预留"接入 mcp_servers 时再评估表语法" | 扩展数组元素支持**内联表**；新增 `Config.mcp_servers` 字段与校验 |
| `mncc/cli.py` | `build_registry()` 装配 6 个本地工具；`main()` 无生命周期钩子 | `attach_mcp_tools(registry, config)` 并入远端工具；`main()` 出口统一 `close()` |
| `mncc/tools/base.py` | Tool 抽象 + ToolRegistry（M3 定型） | **不碰**——`McpTool(Tool)` 在 mcp 包内实现，直接复用该抽象 |
| `mncc/agent/`、`safety/`、`llm/`、`ui/` | — | **一律不碰** |
| `.github/workflows/ci.yml` | 不存在 | 新增（ruff + pytest + bench mock 冒烟） |
| `README.md` | 无 badge、无 MCP 章节、无架构图 | 补 CI badge、mermaid 架构图、MCP 配置段、安装命令、设计决策与局限、路线图 M6 勾选 |
| `INTERVIEW.md` | 不存在 | 新增（面试考点清单） |
| `bench/results/` | gitignore 已忽略 | 不动 |
| `tests/` | 254 项 | 新增约 20 项（全部无真实网络依赖） |

关键既有契约（必须遵守）：
- 依赖白名单（§3）：第三方库仅 openai/rich/prompt_toolkit/pytest/ruff——**MCP 手写走标准库**，不引入 `mcp` SDK（PROMPT §5.A 明示"手写 JSON-RPC over stdio 可加分"）；
- `registry.openai_schemas()` 是 loop 请求模型的唯一入口：远端工具注册进**同一个** registry 即自动具备 function calling + 确认门禁 + 错误回填，loop.py 零改动；
- `needs_confirm` / `--yolo` 语义：本地 write_file/run_command 已按此模型运转，MCP 工具沿用同一套门禁；
- Windows + Git Bash 子进程统一 UTF-8；路径处理用 pathlib。

## 2. 已拍板的决策（含理由，面试点）

**D1 手写 JSON-RPC over stdio 最小子集（协议版本 2024-11-05），不引入 mcp SDK。**
只实现 `initialize` / `notifications/initialized` / `tools/list` / `tools/call` /
`shutdown`。为什么：项目定位是"零 Agent 框架手写"，MCP 手写与之一致，且面试点
（§8 备忘）就是"MCP 握手流程 + framing"——用 SDK 就把这个深水区外包了。成本评估：
stdio framing（Content-Length 头）约 30 行，四个方法约 100 行，可维护。协议版本 2024-11-05
足够对接官方 filesystem server（其支持版本协商）；echo server 自写，无兼容压力。
未来若需更全协议，McpClient 接口隔离，替换成 SDK 也不动上层。

**D2 mcp_servers 走配置：扩展现有 TOML 子集解析器支持内联表，Config 新增字段。**
格式：`mcp_servers = [{ name = "echo", command = "python", args = ["-m", "mncc.mcp.echo_server"] }]`
放全局 `~/.mncc/config.toml`（项目 `.mncc.toml` 可覆盖，沿用合并链）。为什么扩展现有的
`parse_toml_subset` 而不是引入 tomli：白名单没有 tomli，且只需要内联表这一种新语法
（M5_DESIGN 预留的评估点，现在定案为"扩展子集"）。校验：name/command 必须非空字符串、
name 限 `[a-z0-9_-]+`（命名空间注入防护）、args 为字符串数组。Config.mcp_servers 存
`tuple[dict]` 原始结构，由 cli 层转换为 McpServerConfig——避免 config.py 反向依赖 mcp 包。

**D3 远端工具以 `mcp__<server>__<tool>` 命名注册进现有 ToolRegistry，needs_confirm 默认 True。**
McpTool 实现 Tool 抽象：name 动态拼接；description/schema 直接取自 `tools/list` 返回的
`inputSchema`（本就是 JSON Schema，与本地工具契约一致）；`run(**kwargs)` 代理转发
`tools/call`。为什么确认默认开启：远端 server 的副作用（写文件、跑命令）对用户是黑盒，
REPL 下先预览确认更安全；`--yolo` 与本地工具一样跳过（自动评测/脚本不受阻）。
命名空间前缀防冲突（本地工具永远不叫 mcp__*）。

**D4 生命周期：main() 启动时 attach，出口统一 close；连接失败的 server 跳过并警告，不拖垮主流程。**
每个配置的 server 一个子进程（Popen stdin/stdout 管道）；`attach_mcp_tools` 逐台
connect+list_tools，任何一台失败（进程启动失败/握手超时/协议错误）都只在 stderr 打一条
警告并把该台标记为不可用，其余照常可用。`main()` 用 try/finally 保证 REPL/-p 两种退出都
关闭全部 client（shutdown 请求 + terminate）。为什么 finally 而不是 atexit：进程内可确定的
释放点，避免 Z 进程；-p 模式一次任务就跑，资源清理不能交给 GC。

**D5 MCP 请求同步阻塞 + 单请求超时（默认 30s）；close 强杀不等待。**
与 agent 串行执行模型一致（一问一答），不需要并发。每个请求读响应带超时，超时抛
McpError → 经 Tool 层转 is_error 回填，模型可见"远端超时"并能自纠。close 时先发
shutdown（带 5s 超时），再 terminate+wait，防止僵尸。

**D6 CI（GitHub Actions）：ruff + pytest + bench mock 冒烟，全部不碰真实 API。**
workflow 在 ubuntu-latest + 官方 setup-python；三步骤：`pip install -e ".[dev]"`
→ `ruff check .` → `pytest` → `python bench/runner.py --mode mock`。README 顶部挂
`actions/workflows/ci.yml` badge。为什么 bench 只跑 mock：CI 无 key，而 cassette 冒烟
（M5 D5）本就是为了"无 key 守护管线不腐坏"。

**D7 发布完成标准 = GitHub Release + `pipx install git+https://github.com/wanming1122/mncc.git`；PyPI 后置可选。**
pyproject 已具备 console script（`mncc = mncc.cli:main`）与 setuptools 打包配置，git 安装
零额外工作。GitHub Release 打 tag 即可作为"发行版"证据；PyPI 需要账号+token，列为可选
后续项不在 M6 完成标准内（验收 6 有替代路径）。

**D8 测试全 mock/自写 server：零外部工具依赖。**
MCP 相关测试三类：(a) framing 纯函数测试（协议层）；(b) stub server 子进程回放
初始化/list/call/超时/进程清理（client 层，用 `python -c` 起一个临时脚本进程）；
(c) 项目自带的 echo server 端到端（`python -m mncc.mcp.echo_server`）。echo server 用
标准库手写（读帧→回 JSON-RPC 响应），同时是 §5.A 验收工具——不依赖 npx/Node，Windows
本机即可验收。真实 filesystem server 接入列为"可选加强验收"（需用户本机有 Node）。

## 3. 模块设计与签名

### 3.1 `mncc/mcp/protocol.py`（新增，约 40 行）

```python
def encode_message(payload: dict) -> bytes:
    """JSON-RPC over stdio 帧编码：json.dumps + Content-Length 头。"""

def decode_message(stream: io.BufferedReader) -> dict | None:
    """读一帧：解析 Content-Length 头，读满 body，返回 dict；EOF 返回 None。"""

def make_request(method: str, params: dict, _id: int) -> dict: ...
def make_notification(method: str, params: dict) -> dict: ...
```

面试点：framing 为什么是 `Content-Length: N\r\n\r\n<body>`——JSON 本身含 `\r\n`，
行分隔不可靠，必须显式长度；UTF-8 下长度按字节数计算。

### 3.2 `mncc/mcp/client.py`（新增，核心）

```python
class McpError(Exception): ...   # 连接/协议/超时统一出口，message 面向模型可读

@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()

class McpClient:
    server: str                          # 配置名（唯一性由调用方保证）

    def __init__(self, cfg: McpServerConfig, *, timeout: float = 30.0) -> None
    def connect(self) -> None: ...       # Popen → initialize → notifications/initialized
    def list_tools(self) -> list[dict]:  # -> [{name, description, inputSchema}]
    def call_tool(self, name: str, arguments: dict) -> str: ...  # 结果序列化成可读文本
    def close(self) -> None: ...         # shutdown(5s) → terminate → wait
    @property
    def alive(self) -> bool: ...

class McpTool(Tool):
    name = "mcp__<server>__<tool>"       # 动态拼接
    def __init__(self, client: McpClient, server: str, tool_name: str, spec: dict) -> None
    # description/schema 取自 spec["description"] / spec["inputSchema"]
    def run(self, **kwargs) -> str: ...  # 代理转发 client.call_tool
    def needs_confirm(self, args) -> bool: return True   # D3

def attach_mcp_tools(registry: ToolRegistry, servers: list[McpServerConfig]) -> list[McpClient]:
    """逐台 connect + list_tools + 注册；失败的 server 打印警告跳过。
    返回成功连接的 client 列表（供调用方统一 close）。"""
```

`call_tool` 结果文本化规则：`tools/call` 返回 `content: [{type:"text", text}]` /
`isError` → 拼接文本或带 [error] 前缀；`structuredContent` 兜底 json.dumps。

### 3.3 `mncc/mcp/echo_server.py`（新增，验收/测试工具）

`python -m mncc.mcp.echo_server` 启动：stdio 循环读帧，响应 `initialize`
（protocolVersion=2024-11-05，capabilities={}）、`tools/list`（一个工具：
`echo(say: string)`，返回 say）、`tools/call`（回显参数）。纯标准库，约 60 行。
作用：单测端到端 + 用户验收 REPL 里 `mcp__echo__echo`（无需外部工具）。

### 3.4 `mncc/config.py`（唯一核心改动点之一）

```python
# parse_toml_subset 数组元素扩展：允许 { k = v, ... } 内联表（值仍限既有标量子集）
Config.mcp_servers: tuple[dict[str, object], ...] = ()
# load_config 校验：name/command 非空字符串、name 匹配 ^[a-z0-9_-]+$、
# args 为字符串列表；未知/类型错报 ConfigError（带行号的现有风格）
```

### 3.5 `mncc/cli.py`（唯一核心改动点之二）

```python
def build_registry(root: Path | None = None) -> ToolRegistry:
    ...  # 现有 6 本地工具不变

def _connect_mcp(registry, servers) -> list[McpClient]:
    # cli 层把 Config.mcp_servers(tuple[dict]) 转成 McpServerConfig，调 attach_mcp_tools

def main(argv=None) -> int:
    ...
    clients = _connect_mcp(registry, config.mcp_servers) if config.mcp_servers else []
    try:
        ...  # 现有 -p / REPL 分派不变
    finally:
        for client in clients:
            client.close()
```

不新增 CLI 旗标（配置驱动，与 M5 D3 的"不新增旗标"一致）。

### 3.6 `.github/workflows/ci.yml`（新增）

```yaml
name: CI
on: [push, pull_request]
jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: python -m pip install -e ".[dev]"
      - run: python -m ruff check .
      - run: python -m pytest -q
      - run: python bench/runner.py --mode mock
```

### 3.7 `INTERVIEW.md`（新增）与 `README.md`（补全）

INTERVIEW.md 大纲（PROMPT §10 列出的 9 个考点 + 本项目特有追加）：
1. Agent Loop 状态机终态（completed/max_turns/budget/interrupted/error）与退出码契约
2. tool_calls 消息结构（OpenAI 协议、role=tool 回填结构合法性）
3. 流式下工具调用增量分片如何聚合（客户端聚合 vs 业务层感知）
4. 两级压缩的信息损失权衡（L1 截断 vs L2 摘要；auto 与手动共用一实现）
5. token 估算误差来源与在线校准
6. MCP 握手流程 + framing（Content-Length 帧、initialize→initialized→list→call→shutdown）
7. 评测方法论：成功率定义（客观 pytest vs 模型自报）、防 flaky（确定性任务 + 单跑不重试）、
   样本量局限（20 任务是趋势不是显著性）、成本/规模权衡；--stats-json 机器契约设计
8. 框架 vs 手写（为什么零框架；MCP 为什么也手写）
9. `-p` 与 REPL 架构分叉（输入/确认/输出三差异，共用 loop）
10. M5 迭代负样本：一条无效 prompt 纪律的完整验证过程（如何用数据说"不"）

README 补全：顶部 CI badge；「配置」加 mcp_servers 示例；「使用」加 `mcp__` 工具说明；
「架构」mermaid 图（含 mcp 包）；「安装」补 `pipx install git+URL`；新增「设计决策与局限」
段落；路线图 M6 勾选。

## 4. 测试计划（已实现 **+50 项**；全部无真实 API；现有 254 项不回归）

| 文件 | 覆盖 |
|---|---|
| `tests/test_mcp_protocol.py` | encode/decode 往返、跨 chunk 读帧、Content-Length 头格式、非法帧抛错、EOF 返回 None |
| `tests/test_mcp_client.py` | stub server（python -c 临时脚本）回放：握手成功 / 握手超时 / list_tools 解析 / call_tool 文本化 / isError / 请求超时 / close 后子进程退出（alive=False） |
| `tests/test_mcp_echo.py` | 真实 echo server 子进程端到端：connect→list(echo)→call→close |
| `tests/test_mcp_tool.py` | McpTool 挂 registry、`mcp__` 命名唯一、needs_confirm=True、schema 透传、错误回填 is_error |
| `tests/test_config.py` | （补）mcp_servers 内联表解析、非法 name/command/args 报 ConfigError、数组含非内联表元素报错 |
| `tests/test_cli.py` | （补）`_connect_mcp` 空列表返回 [ ]；attach 生命周期：成功连接被 close（用 fake 转） |

注：`bench/` 20 任务与 mock 冒烟均不涉及 MCP；version verify_m3.py 保持不动（不强行扩层）。

## 5. 验收（PROMPT §5.A/§10 对应条目）

1. 新增测试 + 既有 254 项全绿；ruff 零报错
2. echo server 端到端单测通过（`python -m pytest tests/test_mcp_echo.py`）
3. **本机验收（无需外部工具）**：`~/.mncc/config.toml` 配置 echo server 后，
   `mncc -p "调用 mcp__echo__echo 说你好" --yolo` 退出码 0 且回复里含回显；REPL 里
   同名工具可交互调用（确认后执行）
4. **可选加强**（需 Node）：接官方 `filesystem` server 在 REPL 里调用 `mcp__filesystem__read_file`
5. push 后 GitHub Actions 首跑全绿，README badge 亮
6. `pipx install git+https://github.com/wanming1122/mncc.git` 安装后 `mncc --version` 可用
   （或本地 `pip install -e .` 后 `mncc --version` + `python -m mncc` 验证）
7. INTERVIEW.md 就位；README 补 badge/架构图/MCP 章节/安装；路线图 M6 勾选
8. M6_DESIGN.md 状态行更新

## 6. 开发协议（新对话必须遵守）

1. 先向用户简要复述本设计（尤其 D1–D8），用户确认或修改后再编码
2. mncc/ 核心只动 config.py（mcp_servers+内联表）与 cli.py（attach+close）；loop/safety/llm/ui 不碰
3. 零新第三方依赖；MCP 全部标准库手写
4. 每个已完成单元：先跑 `python -m pytest -q` 与 `python -m ruff check .` 再继续
5. 完成后 git 提交、推 GitHub（用户眼皮子底下触发 CI），更新 README/INTERVIEW/M6_DESIGN，
   给出验收清单
6. 已选模块：§5.A MCP 客户端；§5.B/§5.C 不在本期

## 7. 面试点备忘（INTERVIEW.md 直接素材）

- MCP over stdio 的 framing 细节：为什么长度头、为什么按字节数、跨 chunk 重组；
- 握手序列的协议语义（initialize 的版本协商 → initialized 通知时序）；
- 手写 vs SDK 的工程权衡（本项目的"零框架"定位 vs 协议演进成本）；
- 工具命名空间的隔离价值（mcp__ 前缀防冲突）与远端副作用默认确认的安全立场；
- 与本地工具共用一条抽象链（Tool/ToolRegistry/确认门禁/错误回填）——MCP 不是另一套体系；
- 生命周期管理（finally 关闭、超时强杀、僵尸防护）是"接入外部进程"类功能的通用面试题；
- 评测联动：CI 用 mock 冒烟守管线（M5 cassette 哲学的延续），真实能力评估仍在本机；
- 诚实数据惯例延续：任何迭代改动都以 README 对比数据说话（M5 的负样本是范例）。