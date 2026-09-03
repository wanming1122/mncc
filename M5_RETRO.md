# M5 复盘：bench 20 任务评测 + 首次跑分 + 评测驱动迭代

> 写作日期：2026-09-03。事实来源：`M5_DESIGN.md`、`README.md` 跑分节、`git log`
> （M1–M5 压缩于 `2e305aa`，M5 无独立提交）、`bench/` 代码与 `tests/`。
> 文档未记录处已标注「（对话记录）」——即开发会话中的实测输出与临时操作。

## 1. 目标与范围

把 PROMPT §2 的简历卖点"Agent 成功率从基线 __% 迭代到 __%"变成可复现的系统：
`bench/tasks/` 20 任务（bugfix×6 / feature×6 / refactor×4 / testwrite×4；易 6 / 中 9 / 难 5）、
`bench/runner.py` 跑分管线、`bench/report.py` 跑分/对比表、`mncc -p` 的 `--stats-json`
记账，加首轮真实跑分与一轮 prompt 迭代。**核心只动一处**：`cli.py` 新增 `--stats-json`
（其余核心文件一律不碰）。明确不做：CI / PyPI / INTERVIEW.md（M6）、§7 P1 功能。
零新第三方库——bench 全部标准库 + 既有 rich。

## 2. 开发过程

顺序：**盘既有契约 → 设计 D1–D8 → 核心改动 → 20 任务 → runner/report → 测试 → mock 冒烟 → 真实跑分 → 迭代**。

1. **先盘契约**（`M5_DESIGN.md` §1）：`LoopResult`（status/turns/usage/elapsed/content）自
   M2 定型，本就是为评测记账预留的接口；`mncc -p` 退出码契约（0/1/130）、TOML 子集
   解析器、verify_m3.py 的"子进程 + PYTHONPATH + 临时目录"模式全部可复用。**为什么先盘**：
   评测的判分口径（D1）与记账通道（D4）必须先于任务设计定，否则 runner 会写出第二套不一致逻辑。
2. **设计拍板 D1–D8** 落 `M5_DESIGN.md`，经用户确认。
3. **唯一核心改动**：`--stats-json`（初版"成功才落盘"；真实跑分事故后改为"graceful 全落盘"，见 §4）。
4. **编 20 任务**，3 个 smoke 任务手工编排 `cassette.json`（参数针对 fixture 逐字符对齐），
   比编写时逐个评审 task.md 不泄漏 test.py 内容（§7 开发协议 5）。
5. **runner/report**：runner 按"临时目录→拷贝→注入配置→子进程→评分"落地，report 输出
   README 可粘贴 markdown。
6. **测试 + mock 冒烟**先行（无 API 即可守管线），再以本机 key 跑真实单任务与全量。
7. **一轮迭代**：基于基线的"验证后仍有无关改动"观察（分析）加 prompt 纪律 7「做完即止」→
   重跑 → 数据不支持 → 回滚（负样本入库，见 §4/§6）。

## 3. 关键设计决策

| 决策 | 内容 | 被放弃的替代方案 |
|---|---|---|
| D1 判分口径 | 成功 = 终态 `pytest test.py` 全绿（`verified`）；`exit_code` 只记录不判分 | 以 `mncc` 退出码判分（放弃：模型可"幻觉完成"，D1 实证见 §4） |
| D2 考卷隐藏 | runner 跑 agent 时不拷 `test.py`，结束后才拷入评分 | 开局即给 test.py（放弃：照抄测试当实现是已知作弊形态） |
| D3 隔离与注入 | 每任务独立临时目录 + 子进程；`max_turns` 按难度注入 `.mncc.toml`（易15/中20/难25） | 进程内跑循环（放弃：cwd/环境/输出流不隔离）；新增 CLI 旗标（放弃：复用既有合并链，零新配置面） |
| D4 记账契约 | `-p --stats-json`：LoopResult 落盘机器可读 JSON；M5 唯一核心改动 | runner 解析 stdout/stderr 判分（放弃：渲染输出是脆耦合） |
| D5 mock 冒烟 | per-task `cassette.json` + 进程内 ScriptedClient 回放，走完整 `main()`；结果标记 mode=mock 不进统计 | 录制真实跑当 cassette（放弃：需 API 且非"黄金路径"）；直接调 run_agent_loop（放弃：冒烟要覆盖参数解析→循环→落盘全链路） |
| D6 报表一等公民 | `report.py` 读 results JSON 生成跑分表/对比表；results 含 git_head/label/config 快照 | 手工算表（放弃：PROMPT §2 的对比数据必须可复现、可追溯） |
| D7 任务分布 | 四类任务统一"`pytest test.py` 过即成功"，保法不同：行为断言 / refactor 可见测试初始即绿+扩展断言 / testwrite 元测试（全绿 + 杀死变异体） | 按类别写不同评分逻辑（放弃：runner 无分支，同构契约） |
| D8 防 flaky | 单跑不重试、不静默刷分；不可复现失败在 README 脚注标注 | 失败任务重跑刷分（放弃：违背评测方法论，答不出面试追问） |

## 4. 难点与踩坑

`M5_DESIGN.md` 未设难点章节，以下为代码痕迹与开发记录还原（已标注）：

- **服务端中途断供 → 17 任务 0 token 秒败**（对话记录）。现象：首轮基线跑到一半，
  后续任务 2.7~3.0s 即 fail、轮数/token 全 0、error 只截到 rich 表格边框一行。
  定位：stats 全 0 + stderr 尾部无信息量 → 无法归因是 API 挂了还是模型问题。解决：
  (a) 整轮作废重跑（存档 `real-20260902-192455.server-outage.json.bak`，README 脚注声明，
  不逐任务重刷——D8 纪律）；(b) 修 `--stats-json` 契约：**graceful 退出（含失败）全部落盘**，
  status 字段区分终态，失败任务的轮数/token 同样是归因数据（timeout 强杀仍由 runner 记）；
  (c) runner 的 error 改记 stderr 尾部多行（最后一行常是 rich 边框）。防复发：评测记账
  粒度必须覆盖"失败"本身，D4 契约初版的"成功才落盘"是合规的空洞。
- **评分 test.py 自身假绿/假红**（对话记录，临时脚本验证后删除）。写了 (A) 空操作必须全 fail、
  (B) 参考实现必须全绿的双向健康校验，抓到两处 bug：`ft_timespan` 测试用例笔误
  （`PT1H30M2S` 其实是合法输入，换真非法 `PT1H30M2`）；`ft_todo_cli` 评分子进程在
  Windows GBK 下中文乱码导致误判。防复发：任务编写后必须先过"评测的评测"，再上真实跑分。
- **Windows GBK 乱码三连**（对话记录）：`ft_todo_cli` / 4 个 testwrite 元测试的评分子进程
  补 `PYTHONUTF8=1`；`bench/runner.py`、`report.py` 入口加 `_force_utf8_stdio()`
  （rich 表格 `✓/✗` 在 GBK 控制台直接 UnicodeEncodeError）。这是 M3 同族跨平台问题的延续，
  正片（CI Linux）在 M6 才根治。
- **迭代 1 的"幻觉完成"实证**（README 可查证）：加「做完即止」纪律后重跑，`ft_todo_cli`
  （hard）22 轮、99.5k tokens、`mncc` 退出码 0 但评分 pytest 挂——耗时最多的一题反而假成功，
  恰好实证 D1"不能信模型自报"。整体 100%→95%、tokens +27%，无收益，回滚。

## 5. 验收与数据

- 测试：232 → **254 全部通过 + 2 跳过**（新增 22：`test_bench.py` 19 + `test_cli.py` 3），
  ruff 零报错（`M5_DESIGN.md` 状态行）。
- mock 冒烟：3 个 smoke 任务（bf_calc_ops / ft_csv_stats / tw_str_utils）cassette 回放全 pass，
  17 个无 cassette 任务正确 skipped（`bench/results/mock-20260902-193259.json` 可查证）。
- 任务健康校验：20 任务 (A) 空操作全部正确判 fail、(B) 以参考实现全部判 pass（对话记录）。
- 单任务真实验收（§11 场景 6）：`--tasks bf_calc_ops` pass，11 轮 / 32,082 tokens / 45.1s
  （`bench/results/real-20260902-192104.json` 可查证）。
- **基线**（README，模型 mimo-v2.5）：20/20，**pass_rate 100%**，总 591,382 tokens，
  平均 8.2 轮 / 任务（按 `real-20260902-195602.json` 验算），各难度/类别均 100%。
- **迭代 1**（README）：100% → **95%**（19/20），总 tokens +27%（750,448）；结论无效已回滚，
  对比表由 `report.py bench/results/real-20260902-195602.json real-20260902-202144.json --md` 可再生成。

## 6. 一分钟面试讲述版

M5 我自建了一套 20 任务的代码修改基准和评测管线。判分不信任模型自报：`mncc` 退出码
只是参考，终态 `pytest test.py` 全绿才算过——有一次迭代我亲眼看到模型烧了 9.9 万 token
自报成功、测试却挂，正好证明这个设计。评分测试全程对 agent 隐藏，防"照抄测试当实现"；
每任务独立临时目录 + 子进程隔离，按难度限轮数。还有一个无 API 的 mock 冒烟：把固定
tool_calls 序列录成 cassette 回放，CI 没有 key 也能守管线不腐坏。真实跑分基线 20/20，
我又做了一轮 prompt 迭代，实测通过率反而降到 95%、token 涨了 27%——于是按数据把改动
回滚，把这次"失败迭代"写进 README。评测的纪律就是：成功率以客观断言为准、单跑不重试、
数据说话、趋势不是显著性。

## 7. 延伸与建议

- **重做会改什么**（分析）：基线 100% 说明 20 个任务对 mimo-v2.5 已无失败压力，区分度不足；
  应保留 3–5 个"压过了难度上限"的题目让基线落在 80% 左右，迭代才有比较空间；cassette 只
  覆盖 3 个任务，冒烟面偏窄；真实跑分成本 ≈59 万 token，可在保持单跑纪律下压缩困难任务的
  注入轮数上限。
- **面试追问**："样本量 20 有意义吗？"→ 定位是趋势与回归防线，不是显著性检验；单任务
  失败也能定位 prompt 改动的影响点。"失败为什么不能重跑刷分？"→ 重试即作弊，破坏
  与真实使用一致的单次执行假设。"test.py 藏起来，模型不知道要跑测试怎么办？"→ task.md
  用自然语言描述症状与"运行验证"要求，正是把验收标准写进需求的真实形态。
- **进阶方向**：bench 引入"回归演进集"（每轮失败任务沉淀为新用例）；把 MCP 工具接入
  同一评分模型（M6 之后的自然延伸）；多次采样跑分求通过率方差，把 D8 的"确定性"主张
  从纪律升级为分布数据。

---

（项目总览表见 M6_RETRO 末尾。）