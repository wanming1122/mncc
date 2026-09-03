# M2 设计文档：工具调用闭环（已确认）

> 状态：设计已评审确认（2026-09-02，M1 会话末尾），编码完成（2026-09-02）。112 项测试全部通过，ruff 零报错。
> 范围：PROMPT.md §12 M2——只做 read_file / write_file / run_command 三个工具，打通工具调用闭环。
> 边界：路径守卫、命令黑名单、diff 预览渲染放 M3（独立切面一次性做全，避免半套守卫造成虚假安全感）。

## 已拍板的决策

1. **`-p` 非交互模式下 write_file 默认拒绝**：遇到写入确认时报错并提示加 `--yolo`；
   M5 的 bench runner 统一带 `--yolo`。理由：`-p` 常被脚本化调用，静默写文件不可接受。
2. **tool_calls 流式增量在客户端内聚合**（不向 loop/UI 暴露协议碎片）。
3. **确认交互做成回调注入**，工具本身无 IO。
4. **工具报错不抛异常**，以 is_error 标记回填 role=tool，让模型自纠。

## 1. 协议层 `llm/client.py`

```python
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # 原始 JSON 串；解析失败作为错误回填，让模型自纠


@dataclass
class ResponseCompleted(Event):
    content: str
    finish_reason: str = "stop"
    usage: Usage | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)  # 新增
```

为什么聚合放客户端：流式协议里 tool_calls 按 index 分片交错到达（id/name/arguments 碎片），
聚合逻辑与协议强相关，留在 openai_compat.py 聚合完再交给上层，loop/UI 不需要懂协议。
代价是 UI 看不到"参数输入中"的中间态，M2 只需要"正在执行 xxx"，够用。

实现要点（openai_compat.py）：按 chunk.choices[0].delta.tool_calls 的 index 聚合
id/name/arguments 片段；finish_reason == "tool_calls" 时在 ResponseCompleted 带上完整列表。
Message 类型已是宽松 dict，assistant 消息回填 tool_calls 时用 OpenAI 标准结构：
`{"role": "assistant", "content": str|None, "tool_calls": [{"id", "type": "function", "function": {"name", "arguments"}}]}`。

## 2. 工具抽象 `tools/base.py`

```python
@dataclass
class ToolResult:
    output: str
    is_error: bool = False

class Tool(ABC):
    name: str
    description: str
    schema: dict                                  # JSON Schema（parameters 部分）
    def run(self, **kwargs) -> str: ...
    def needs_confirm(self, args: dict) -> bool: return False

ConfirmFn = Callable[[Tool, dict], bool]          # 注入的确认回调

class ToolRegistry:
    def register(self, tool: Tool) -> None: ...
    def get(self, name: str) -> Tool | None: ...
    def openai_schemas(self) -> list[dict]: ...   # [{"type":"function","function":{...}}]
    def execute(self, name: str, arguments_json: str, *,
                confirm: ConfirmFn, yolo: bool) -> ToolResult
```

execute 职责：解析 JSON（失败→ is_error + 让模型重发）；未知名→ is_error 列出可用工具；
needs_confirm 且非 yolo 且 confirm 拒绝→ is_error("用户拒绝")。
为什么错误回填而非抛异常：模型必须看到错误原文才能自纠重试，抛异常终止循环
就杀掉了"自主恢复"——agent 可靠性的核心。

## 3. 三个工具 `tools/files.py`、`tools/command.py`

- `read_file(path, offset?, limit?)`：输出带行号；默认最多 2000 行，超出截断并提示用 offset 分页；
  文件不存在/是目录→ is_error 友好提示
- `write_file(path, content)`：needs_confirm=True（--yolo 跳过）；父目录不存在时自动创建
- `run_command(cmd, timeout=30)`：subprocess；**Windows 下统一 UTF-8**（encoding="utf-8",
  errors="replace"，防 GBK 乱码）；超时强杀（proc.kill()）返回 is_error；输出合计超长截断
  （保留首尾）；返回 stdout/stderr/exit_code 三段式文本

路径守卫（穿越拒绝）M3 补——本里程碑不伪造安全感，代码里不留 TODO 注释，
直接在 M3 的 safety/guard.py 一次做完。

## 4. 循环扩展 `agent/loop.py`

```python
@dataclass
class LoopResult:
    status: str          # completed | max_turns | budget_exceeded | interrupted | error
    turns: int
    total_usage: Usage
    elapsed: float
    content: str         # 最后一条 assistant 文本（汇报用）

def run_agent_loop(client, renderer, session, registry, *,
                   max_turns: int, token_budget: int,
                   confirm: ConfirmFn, yolo: bool) -> LoopResult
```

循环体：add_user → 每轮 stream → ResponseCompleted.tool_calls 非空：
逐个（tool_progress 显示 → confirm → execute → 回填 role=tool，tool_call_id 对应）→ 下一轮；
无 tool_calls → completed。中止条件：轮数达 max_turns（默认 25）；
累计 token 超 token_budget（默认 200k）→ status=budget_exceeded（§4.2）；
KeyboardInterrupt → interrupted，已完成的工具结果照常回填保持消息结构合法。

为什么 LoopResult 现在就定 turns/usage/elapsed：M5 bench 要记录"成败/轮数/token/耗时"，
接口先定型避免返工。M1 的 run_turn 保留给"纯对话"用途或直接合并进新循环（实现时定，
倾向合并为一个函数，REPL 与 -p 共用）。

## 5. System prompt 与 UI

- `prompts/system.py` 升级为 §9 完整版（先读后改 / 小步前进 / 不猜文件 / 诚实汇报 / 结束总结），
  每条规则在注释里写清解决什么实际问题。为什么 M2 就写全：M5 评测驱动迭代打磨的对象
  就是这份文案，越早进入真实迭代越能积累对比数据。
- `ui/render.py` 新增：`tool_progress(name, brief)`（"正在读取 xxx"，单行覆盖式不刷屏）、
  `confirm_write(path, preview) -> bool`（y/n 交互）。
- `cli.py`：`--yolo` 贯通到 registry.execute；REPL 的 confirm 用 rich 的 y/n；
  `-p` 的 confirm 一律拒绝（决策 1）。

## 6. 测试计划（LLM 全 mock）

- openai_compat：tool_calls 分片聚合（交错 index、arguments 多段拼接、finish_reason=tool_calls）
- registry：JSON 解析失败回填、未知名回填、confirm 拒绝回填、--yolo 跳过
- loop：完整闭环（fake client 回放 tool_calls 序列）、max_turns 中止、budget_exceeded、
  中断后消息结构合法、工具 is_error 后模型继续（脚本化第二轮自纠）
- 三工具单测：read 分页/行号、write 确认与父目录创建、run_command 超时强杀/UTF-8/截断
- 集成：`mncc -p` 带工具的端到端（脚本化 client），退出码正确

## 7. 验收（对应 §11）

1. 修 bug 场景：样例仓库输入"测试挂了，找到 bug 修复并让全部测试通过"→
   agent 自主 read → edit → run_command(pytest) → 汇报
2. `-p` 模式跑通上述场景，退出码 0；write 场景不带 --yolo 被拒（退出码非 0）
3. REPL 中工具调用过程显示"正在执行 xxx"，完成后有总结汇报
