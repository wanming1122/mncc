# M3 设计文档：工具集补全 + diff 预览 + 路径/命令守卫（已确认）

> 状态：设计已评审确认（2026-09-02），编码完成（2026-09-02）。200 项测试收集（其中 2 项
> 符号链接用例在无权限环境自动 skip），全部通过，ruff 零报错。
> 范围：PROMPT.md §12 M3——edit_file / list_dir / grep 三工具；`safety/guard.py`
> 路径守卫 + 命令守卫；rich diff 确认交互。
> 边界：检查点回滚是 §5.C（M6）；上下文压缩是 M4；黑名单不进配置文件（硬编码常量，
> 可独立审计）；run_command 不走路径守卫（shell 无法被路径分析可靠沙箱化，诚实边界）。

## 已拍板的决策

1. **黑名单绝对拦截，`--yolo` 不解锁**：黑名单（rm 带 -r/-f、del /s、mkfs、format 盘符、
   curl|sh、dd 写 /dev/、fork 炸弹）是不可逆灾难命令，命中即以 is_error 回填模型并提示
   替代方案。`--yolo` 跳过的是"确认"，不跳过"红线"——门禁强度不同。
2. **命令授权按"精确命令串"会话级记忆**：批准 `python -m pytest` 不连带放行
   `python -c "os.remove(...)"`。授权粒度等于被授权对象。
3. **edit_file 行尾保留**：读 bytes → 检测主导行尾（CRLF/LF）→ 内存按 LF 匹配替换 →
   按原主导行尾写回。否则 Windows CRLF 文件一编辑就全文件变行尾，git diff 全红。
4. **未命中诊断只提示不自动改**：先查行尾空白差异，再给最相近片段；静默修正会掩盖
   模型的真实错误，评测数据失真。
5. **`SafetyViolation` 独立于 `ToolError`**：safety 模块零项目内依赖（可独立审计）；
   registry 显式捕获并转 is_error，避免"工具执行异常：SafetyViolation…"丑消息。
6. **修复 M2 隐患（D7）**：`execute` 确认段此前无异常防护——M2 的 preview 是 json.dumps
   永不抛错，M3 的 edit preview 要读文件算 diff，会抛错炸穿循环。确认段包进 try，
   预览失败转为 is_error 回填真实原因，让模型自纠。

## 1. `safety/guard.py`（新文件，零项目内依赖）

```python
class SafetyViolation(Exception):
    """守卫拒绝。message 面向模型：说明原因 + 正确做法。"""

class PathGuard:
    """把所有文件操作限制在工作区（启动目录）内。"""
    def __init__(self, root: Path) -> None: ...   # root 在构造时 resolve() 固化
    def resolve(self, path: str) -> Path:
        """相对路径以 root 为基准 → resolve() 展开 ../ 与符号链接
        → 结果必须等于 root 或位于 root 内，否则 raise SafetyViolation。"""

@dataclass(frozen=True)
class CommandVerdict:
    action: str   # "allow" | "confirm" | "block"
    reason: str = ""

class CommandGuard:
    """黑名单必拦截；其余命令首次执行需确认；授权按会话记忆。"""
    def check(self, cmd: str) -> CommandVerdict: ...
    def approve(self, cmd: str) -> None: ...      # 记住已授权的精确命令串
```

## 2. 工具扩展

- `tools/files.py` 新增 `EditFileTool(guard)`：
  - run 流程：guard.resolve → 读 bytes 判主导行尾 → 精确匹配计数：
    0 次 → 空白差异提示（仅诊断）或最相近片段 + mini diff；
    >1 次 → 报错列出各命中行号，要求扩大上下文；恰 1 次 → 替换写回。
  - `needs_confirm=True`；`preview()` 返回 unified diff（rich 用 diff 词法高亮）；
    preview 读文件失败照实抛错（registry 层转 is_error，D7 场景）。
- `tools/search.py`（新）：`ListDirTool(guard)`、`GrepTool(guard)`。
  共享 `DEFAULT_IGNORES`（.git/__pycache__/node_modules/venv/.venv/.mncc/dist/build/*.egg-info）。
  list_dir 上限 500 项/深度 8；grep 上限 100 条，输出 `相对路径:行号:内容`，
  二进制文件（前 8KB 含 NUL）跳过，超长行截断。
- `tools/command.py`：`RunCommandTool(guard: CommandGuard)`；
  `needs_confirm` = check(cmd).action == "confirm"；run 先 check，block 则 ToolError；
  通过后 approve(cmd)。block 检查不受 yolo 影响。
- 三个文件工具与两个搜索工具均注入 `PathGuard`；`base.py` 的 `execute` 增加
  `SafetyViolation` 显式捕获 + 确认段异常防护；`Tool` 基类新增
  `preview_lexer`（默认 ""）与 `confirm_title(args)`。
- `ui/render.py`：`confirm_write` 泛化为 `confirm(title, body, *, lexer=None)`，
  edit 用 `Syntax(lexer="diff")` 高亮。
- `cli.py`：`build_registry(root=None)` 统一构造 `PathGuard(cwd)` + `CommandGuard()`
  并注入六工具；确认回调按 `tool.confirm_title` / `tool.preview` 分发。

## 3. 测试计划（新增约 35 项；LLM 全 mock）

| 文件 | 覆盖 |
|---|---|
| `test_safety.py`（新） | 路径守卫：界内放行（相对/绝对）、`../` 穿越拒绝、绝对界外拒绝、符号链接逃逸（无法建链则 skip）；命令守卫：各黑名单变体必拦、干净命令需确认、approve 后同串免确认、参数变体重新确认、空命令 block |
| `test_tools_files.py` | 既有用例换注入构造；add edit_file：精确替换、多处命中列行号、未命中诊断（相近片段/空白差异提示）、CRLF/LF/无尾换行保留、old==new 拒绝、diff 预览内容 |
| `test_tools_search.py`（新） | list_dir 树形/忽略集/截断；grep 输出格式、glob 过滤、100 条上限、非法正则、二进制跳过 |
| `test_tools_base.py` | preview 抛错 → is_error 不炸循环（D7）；SafetyViolation 显式转为 is_error 而非"工具执行异常" |
| `test_tools_command.py` | 守卫集成：黑名单经 registry 返回 is_error、首次 needs_confirm、approve 后免确认 |
| `test_cli.py` | build_registry 六工具；-p 模式守卫行为不变 |

## 4. 验收（对应 §11）

1. REPL：六工具全可用，edit 前 diff 高亮确认，命令首次执行 y/n
2. §11 场景 4："删掉 D:\ 下所有文件" → 黑名单拦截 → 模型如实汇报拒因
3. `../` 与符号链接穿越均被路径守卫拒绝
4. 全量测试 + ruff 通过