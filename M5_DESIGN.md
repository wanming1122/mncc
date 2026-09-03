# M5 设计文档：bench 20 任务评测 + 首次跑分 + 一轮评测驱动迭代

> 状态：完成（2026-09-02）——bench 管线 + 20 任务 + `--stats-json` 落地；首轮真实
> 跑分基线 20/20（pass_rate 100%，总 591k tokens）；一轮评测驱动迭代（"做完即止"
> 纪律，结果 95% + tokens +27%，无收益已回滚，对比数据写入 README）。
> 本文件是 M5 开发的唯一事实源——新对话中的开发者请先通读，按 §7「开发协议」执行。
>
> 范围：PROMPT.md §6 评测体系 + §12 M5——bench 目录（20 任务/runner/结果落盘/对比报告）、
> `mncc -p` 的 `--stats-json` 小组件、README 跑分表；以及首轮真实跑分 + 一轮
> prompt/工具描述迭代（数据写入 README）。
> 边界：CI（GitHub Actions）、PyPI、INTERVIEW.md 是 M6；§7 P1 功能不在 M5。

## 1. 现状盘点（新对话开发者必读，无需重新探索）

M1–M4 已完成 232 项测试全绿、ruff 零报错。M5 复用现有基建，**mncc/ 核心只有一处改动**：

| 位置 | 现状 | M5 改动 |
|---|---|---|
| `mncc/cli.py` | `-p` 模式返回退出码，`LoopResult`（status/turns/total_usage/elapsed/content）在进程内被丢弃 | **新增 `--stats-json 路径` 旗标**：`-p` 结束时把 LoopResult 落盘 JSON（评测记账所需的最小核心改动） |
| `mncc/agent/loop.py` | `LoopResult` 字段 M2 起定型（M2_DESIGN 就是为 M5 bench 预留的接口） | 不动 |
| `mncc/config.py` | 默认→全局→项目→环境变量→命令行合并链；TOML 子集解析器（字符串/整数/小数/布尔/字符串数组） | 不动（runner 直接复用解析器读任务元数据） |
| `mncc/tools/`、`safety/`、LLM 层、`ui/render.py` | — | **一律不碰** |
| `tests/` | 232 项全绿 | 新增 `tests/test_bench.py`；已有断言不回归 |
| 新建 `bench/` | 不存在 | `runner.py`、`report.py`、`tasks/`（20 任务）、`results/` |
| `README.md` | 无跑分表 | 验收阶段添加跑分表段落（首轮基线 + 迭代对比，数据以实测为准） |

关键既有契约（必须遵守）：

- `mncc -p` 退出码契约：0=completed、1=其余失败、130=interrupted（评测判分依赖）；
- `Session.tokens_estimate` 是未校准静态口径；`LoopResult.total_usage` 是真实累计 usage
  （runner 记账用后者）；
- 依赖白名单（§3）没有新第三方库——bench 全部用标准库 + 已有依赖（rich 渲染表格）；
- Python 3.10 兼容、ruff line-length 100、Windows + Git Bash 下子进程统一 UTF-8；
- `verify_m3.py` 已验证的"子进程跑 `mncc -p` + PYTHONPATH 注入 + 临时目录"模式，就是
  runner 真实模式的雏形。

## 2. 已拍板的决策（含理由，面试点）

**D1 评分口径：成功 = 终态 pytest 全绿（verified），不以 `mncc` 退出码为准。**
两者都记录：`exit_code`（模型自报）与 `verified`（客观判分）。为什么：PROMPT §9
"诚实汇报"纪律的存在本身就说明模型可能"幻觉完成"——退出 0 但测试挂是评测要抓的
典型失败；客观断言是唯一不以模型自评为准的依据。

**D2 评分用 `test.py` 对 agent 全程隐藏。** runner 先拷贝 `init/` + `task.md` 跑 agent，
**结束后**才把 `test.py` 拷入临时目录执行。为什么：防止模型"照抄测试用例当实现"刷分；
也更贴近真实场景（用户只给自然语言需求）。bug 修复类任务的症状全部写在 `task.md` 里。

**D3 每任务独立临时目录 + 子进程隔离；参数注入走项目级 `.mncc.toml`，不新增 CLI 配置旗标。**
runner 拷 `init/` + `task.md` 到临时目录，写入 `.mncc.toml`（按难度收紧 `max_turns`，
如易 15/中 20/难 25），再以 `mncc -p "$(task.md)" --yolo` 子进程执行，硬超时强杀。
为什么子进程而不是进程内循环：cwd/环境变量/输出流天然隔离，任务间零污染；
`.mncc.toml` 复用现有合并链，零新代码。为什么按难度分档 max_turns（易 15/中 20/
难 25）：困难任务需要更多轮数作为"任务上限"；成本（token 规模）靠 fixture 小型化
控制（§4 任务清单：全部小型纯 Python，无网络无时间依赖），两者分开管。

**D4 新增 `mncc -p --stats-json PATH`：LoopResult 落盘。** 子进程模式下内存里的
LoopResult 拿不到，轮数/token/耗时必须显式导出；这是 M5 对核心代码的唯一改动。
flag 仅对 `-p` 有效，REPL 忽略。

**D5 mock 冒烟模式 = per-task `cassette.json` + 进程内脚本化客户端。**
runner 的 mock 模式不 fork 子进程、不碰真实 API：复用 test_cli 的 fake_deps 手法
（monkeypatch `OpenAICompatClient`），把 cassette 回放成 ScriptedClient 事件序列，
驱动完整 `main(["-p", ..., "--yolo", "--stats-json", ...])`。为什么走 main() 而不是
直接调 run_agent_loop：冒烟要覆盖"任务文案 → 参数解析 → 循环 → 结算 → 落盘"整条
bench 管线。cassette 只给 3 个 smoke 任务（其余任务无 cassette，mock 模式会报
"无 cassette 跳过"）；mock 结果标记 `mode=mock`、不进跑分统计。为什么 cassettes
手工写而不是录制真实跑：录制需要真实 API；手工写固定 tool_calls 序列（参数针对
fixture 精确编排）本身也是一次"黄金路径"文档化。

**D6 跑分表与前后对比由 `bench/report.py` 生成，是 M5 的一等公民。**
runner 每次跑落盘 `bench/results/<run_id>.json`（含 `git_head`、`--label` 标签、
config 快照、逐任务明细、汇总）；`report.py` 读单份跑分输出 README 可粘贴的
markdown 表，读两份跑分输出基线 vs 当前的对比表。为什么工具化而不是手工算：
PROMPT §2/§6 的简历卖点就是"__% → __% 的对比数据"，数据管道必须是可复现的
第一等工具；`git_head` 保证"这轮跑分对应哪个 prompt 版本"可追溯。

**D7 任务集分布：20 = bug 修复×6 + 小功能新增×6 + 重构×4 + 测试编写×4；难度
易 6 / 中 9 / 难 5。** 全部纯 Python、确定性、无网络/系统时钟依赖（防 flaky）。
四类任务的评分 test.py 同构（跑 `pytest test.py` 过即 verified），但**保法不同**：
- bug/功能类：对终态代码做行为断言；
- 重构类：`init/` 内自带可见测试（行为契约，初始即绿），评分 test.py 在可见
  测试基础上扩展断言；
- 测试编写类：test.py 是"元测试"——(a) agent 写的测试对正确实现必须全绿，
  (b) 对植入的变异体（mutant）必须至少失败一次（证明测试不是空转），
  否则判负。
统一"`pytest test.py` 过即成功"让 runner 不用按类别分支评分。

**D8 防 flaky 策略：单跑不重试、不静默刷分。** 20 任务每任务单次执行（成本控制）；
确定性靠任务设计保证（D7）；若某任务出现不可复现失败，在结果里标注并在 README
跑分表脚注说明，宁可分数难看也不重跑刷分——评测方法论（PROMPT §10 INTERVIEW.md
考点）里"防 flaky"的答案就是这条纪律。

## 3. 模块设计与签名

### 3.1 `bench/tasks/<task_id>/`（数据，20 个）

```
bench/tasks/
  bf_calc_ops/
    meta.toml        # name="计算器两个 bug"、category="bugfix"、difficulty="medium"、max_turns 可省略（走难度默认）
    init/            # 初始代码（含被藏起来之前的完整可复现场景）
    task.md          # 单段自然语言任务（不含解决方案提示；bug 类症状写在这里）
    test.py          # 评分断言（D2：跑 agent 时不拷入；D7：按类别撰写）
    cassette.json    # 可选；只有 smoke 任务有（D5）
```

`meta.toml` 用现有 `parse_toml_subset` 解析（扁平的字符串/整数即可，刻意不放表语法）。

cassette.json 结构（与测试里 ScriptedClient 的事件序列同构，JSON 序列化）：

```json
[
  [
    {"type": "ResponseCompleted", "content": "",
     "tool_calls": [{"id": "c1", "name": "read_file", "arguments": "{...}"}]}
  ],
  [
    {"type": "ResponseCompleted", "content": "已修复并验证", "usage": {"prompt": 100, "completion": 30}},
    ...
  ]
]
```

### 3.2 `bench/runner.py`（新建）

```python
@dataclass
class TaskMeta:
    task_id: str  # 目录名，全局唯一
    name: str
    category: str  # bugfix | feature | refactor | testwrite
    difficulty: str  # easy | medium | hard


@dataclass
class TaskResult:
    task_id: str
    mode: str  # real | mock
    status: str  # pass | fail | timeout | error
    exit_code: int | None  # mncc -p 退出码（D1：只记录不判分）
    verified: bool  # 终态 pytest 全绿（D1：判分依据）
    pytest_detail: str  # 评分 pytest 的摘要/末行
    turns: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    elapsed: float
    error: str = ""  # timeout/error 时的人类可读说明


def load_meta(task_id: str) -> TaskMeta: ...  # 读 meta.toml
def discover_tasks(only: list[str] | None) -> list[str]: ...
def run_task(task: TaskMeta, *, mode: str, model: str | None, timeout: int) -> TaskResult:
    ...
    # real：临时目录 → init/ + task.md + 写 .mncc.toml → 子进程 mncc -p --yolo
    #       --stats-json → 拷 test.py → 子进程 pytest → TaskResult（D2/D3/D4）
    # mock：无 cassette → 返回 status="skipped"；有 → 进程内 fake client（D5）


def run(mode: str, *, tasks: list[str] | None, label: str = "", model: str | None) -> int:
    ...
    # 逐任务跑 → 汇总（overall/按难度/按类别）→ 落盘 results/<run_id>.json
    # → rich 表格打印 → 退出码 = 全部 pass 则 0
```

run_id 格式：`real-YYYYmmdd-HHMMSS` / `mock-...`，落盘文件内嵌 `label`、`git_head`、
config 快照（max_turns 默认值等）、逐任务 `TaskResult`、汇总 dict。真实模式任务级
配置注入：临时目录写 `max_turns = <难度默认>`（易 15/中 20/难 25）；模型/base_url/
key 全部沿用用户全局配置，`--model` 可选透传给 `mncc -p --model`。

评分执行：临时目录里 `python -m pytest -q test.py`，超时 120s，返回码 0 即 verified。

### 3.3 `bench/report.py`（新建）

```python
def load_run(path: str) -> dict: ...  # results/*.json
def summary_table(run) -> rich.table.Table: ...
def markdown_table(run) -> str: ...  # README 可粘贴
def compare_table(old: dict, new: dict) -> str:
    ...
    # 逐任务 老→新 对比 + 总分/分档/分类 pass_rate 增量


def main(argv) -> int:
    ...
    # report.py <a.json> [b.json] [--md]
    #   单份：总表 + 分难度/分类表；两份：对比表；--md：纯 markdown 输出
```

### 3.4 `mncc/cli.py`（唯一核心改动，D4）

```python
build_arg_parser() 加：
    parser.add_argument("--stats-json", metavar="路径",
                        help="评测用：-p 结束时把 轮数/token/耗时 写入 JSON")
run_print_mode(config, client, task, registry, *, yolo, stats_json=None)：
    结尾调用 _write_stats(stats_json, result)   # result: LoopResult
```

JSON 字段：`{"status", "turns", "total_tokens", "prompt_tokens",
"completion_tokens", "elapsed", "chars"}`。进程能正常退出的所有终态一律落盘
（status 区分 completed/max_turns/budget_exceeded/interrupted/error——失败任务
的轮数/token 同样是归因数据，2026-09-02 首轮基线跑分中服务端中途断供造成的
17 个"0 轮 0 token 失败"正是缺此数据难以归因）；唯一例外是外部强杀
（timeout）：进程没机会写，runner 自己记 timeout。

### 3.5 新增/改动测试

`tests/test_bench.py`（约 15 项，无真实 API）：meta 解析、任务发现、init 拷贝 +
`.mncc.toml` 注入、评分器（对构造好的临时目录跑 test.py，通过子进程 pytest 的
真实返回码）、mock 模式整管线（用 smoke 任务 + cassette 断言 results JSON 落盘
且 verified）、汇总聚合与 pass_rate 计算、report 对比表输出。`tests/test_cli.py`
补 `--stats-json` 落盘断言（fake_deps + capsys）。

## 4. 二十任务清单（ids + 一句话规格；详情在各自 task.md 编写时细化）

| id | 类别 | 难度 | 一句话规格 |
|---|---|---|---|
| bf_calc_ops | bugfix | medium | 计算器减法写成加法、除零不抛异常（PROMPT §11 场景 1 同款，症状写进 task.md） |
| bf_str_join | bugfix | easy | `join_lines` 分隔符用错、空列表未返回空串 |
| bf_off_by_one | bugfix | easy | 分页切片边界错误：第 N 页漏末元素 |
| bf_json_empty | bugfix | medium | JSON 解析器对空串/注释输入抛裸异常而非友好错误 |
| bf_sort_stable | bugfix | medium | 按 (日期, 名称) 双键排序：日期解析错误导致顺序错乱 |
| bf_lru_mutable | bugfix | hard | LRU 缓存装饰器对 list 参数哈希失败/串味 |
| ft_csv_stats | feature | easy | 读 CSV 求每列 mean/min/max（标准库 csv，无 pandas） |
| ft_password | feature | easy | 密码强度校验函数（长度/字符类别规则给定） |
| ft_retry | feature | medium | `@retry(max_attempts, backoff)` 装饰器，可测（注入时钟） |
| ft_slugify | feature | medium | URL slug 生成：中文转拼音首字母跳过、保留 ascii、连字符合并 |
| ft_todo_cli | feature | hard | §11 场景 2：命令行 TODO 应用（增/删/查/完成，argparse + json 存储） |
| ft_timespan | feature | hard | 时间区间解析/格式化（ISO 8601 duration 子集） |
| rf_split_module | refactor | medium | 400 行模块按职责拆 3 文件，保持包级 import 接口不变 |
| rf_dedup | refactor | medium | 两个近似函数（复制粘贴差异）合并为一，行为不变 |
| rf_dataclasses | refactor | hard | 元组+字典传导的结构重构为 dataclass，外部接口不变 |
| rf_compose | refactor | hard | 拆副作用（文件 IO 与纯逻辑分离），保持输出等价 |
| tw_str_utils | testwrite | easy | 为字符串工具函数写 pytest，须杀死 1 个变异体 |
| tw_bank | testwrite | easy | 银行账户类测试（存取边界、负余额、并发无关确定性） |
| tw_date_parser | testwrite | medium | 日期解析器测试：闰年、非法输入、边界 |
| tw_series | testwrite | medium | 数值函数（数列求和）测试：浮点容差、类型边界 |

难度分布核对：easy 6（bf2 + ft2 + tw2）、medium 9、hard 5（bf1 + ft2 + rf2）。
每任务 fixture ≤ 4 个文件、单文件 ≤ ~200 行，控制真实跑分成本。

## 5. 测试计划（约 +18 项；全部无真实 API；现有 232 项不回归）

| 文件 | 覆盖 |
|---|---|
| `tests/test_bench.py` | 任务发现与 meta 解析（含缺 meta 报错）；init 拷贝完整性 + `.mncc.toml` 注入的 max_turns 分档；评分器对 pass/fail 两态的 verified 判定；mock 管线端到端（smoke 任务 + cassette → results JSON 落盘、mode=mock、不进汇总）；无 cassette 任务在 mock 模式下 skipped；汇总聚合（总分/分难度/分分类 pass_rate）；report 单表与对比表内容 |
| `tests/test_cli.py` | `--stats-json`：`-p` 正常完成落盘且字段正确；失败路径不落盘 |
| `tests/test_loop.py` 等 | 不回归 |

注意：`bench/` 下 20 任务本身不是单测对象——它们的"正确性"由 mock cassette 冒烟
（3 个 smoke 任务）与真实跑分验证；编写任务时逐个人工评审 task.md 无答案泄漏。

## 6. 验收（PROMPT §6/§11 对应条目）

1. **§11 场景 6**：`bench/runner.py --tasks bf_calc_ops`（真实 API，单任务）跑通：
   临时目录隔离、自动评分、results JSON 落盘、汇总表打印（本机验证）
2. `bench/runner.py --mode mock --tasks <smoke任务>`：无 API 全管线冒烟通过
3. **首次真实跑分**：20 任务全量，记录基线数据（本轮需用户配合跑，或授权我用
   本机 key 跑）；README 出现跑分表（含 pass_rate、按难度/类别分布）
4. **一轮评测驱动迭代**：调整 system prompt / 工具描述 / max_turns 后重跑，
   `report.py` 输出基线 vs 当前对比，数据写进 README（§11 场景 8）
5. 现有 232 项 + 新增测试全绿；ruff 零报错
6. README：路线图 M5 勾选；状态行更新；M5_DESIGN.md 状态行更新

## 7. 开发协议（新对话必须遵守）

1. 先向用户简要复述本设计（尤其 D1–D8），用户确认或修改后再编码
2. mncc/ 核心只动 cli.py 的 `--stats-json`；其余核心文件不碰
3. bench/ 只用标准库 + 现有依赖；不新增第三方库
4. 每个已完成单元：先跑 `python -m pytest -q` 与 `python -m ruff check .` 再继续
5. 编纂任务时逐个人工评审 task.md：不泄漏测试内容/解法（D2）；重构类任务
   确认可见测试初始即绿
6. 完成后更新本文件状态行、README 路线图与跑分表，并给出验收清单
   （用户跑 `python bench/runner.py --tasks bf_calc_ops` 真实单任务 +
   `python bench/runner.py --mode mock` 冒烟）

## 8. 面试点备忘（写进后续 INTERVIEW.md 的素材）

- 评测方法论：成功率定义（客观 pytest vs 模型自报退出码）、样本量局限（20 任务
  的统计意义是"趋势不是显著性"）、防 flaky（任务确定性设计 + 单跑不重试纪律）、
  成本/规模权衡（fixture 小型化 vs 真实度）
- 为什么 test.py 要隐藏：照抄测试当实现是 LLM 评测中的已知作弊形态；黑盒断言
  更贴近"验收标准只存在于需求里"的真实工程
- `-p --stats-json` 的边界设计：评测记账下沉到产品 CLI 而不是 runner 自行解析
  stdout/stderr——解析渲染输出是脆耦合，数据通道应该是机器可读契约
- 迭代对比的工程化：git_head + label 让"这轮跑分对应哪份 prompt/工具描述"可追溯，
  是"评测驱动迭代"可复现的前提
- cassette 冒烟的价值：CI 无 key 也能守护 bench 管线本身不腐坏（管线回归与模型
  能力评估分离）