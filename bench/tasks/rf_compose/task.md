report_gen.py 的 process_file 把"读文件、统计、渲染报告、写文件"全混在一个函数里。请拆出纯逻辑：

- compute_stats(text) -> dict：纯函数，返回 {"lines", "chars", "words"}（行数 = splitlines 计数；字符数 = len(text)；单词数 = 空白切分计数）
- transform(text, source="") -> str：纯函数（不碰任何 IO），生成报告字符串。source 为文件名标签，非空时报告第一行是 f"报告：{source}"；为空时第一行就是 "报告："
- process_file(path) 保留：读文件 → 用 compute_stats / transform → 写 <path>.report，返回 stats，行为与现在完全一致

约束：可见测试 test_report_gen.py 不许修改且必须保持全绿；transform / compute_stats 必须无副作用。重构后运行 python -m pytest -q 确认。