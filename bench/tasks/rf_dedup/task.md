parse_utils.py 的 parse_a 与 parse_b 是复制粘贴出来的近似重复实现（唯一差异：parse_a 把结果转大写，parse_b 不转——这是历史行为，必须保留）。

请把两份实现合并为一个共用实现 parse(line, upper=False)（upper=True 时结果转大写），parse_a / parse_b 变成薄包装，行为与现在完全一致。

约束：
- 两个函数的对外签名与行为不变；可见测试 test_parse_utils.py 不许修改且必须保持全绿
- 拆分/合并后用 python -m pytest -q 确认