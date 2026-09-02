utils.py 的 join_lines 行为不对，实际观察到的症状：

- join_lines(["a", "b", "c"]) 返回 "a;b;c"（预期用默认分隔符 ", " 连接）
- 调用时传入的自定义 sep 参数没有生效
- join_lines([]) 返回 None（预期空字符串）

修复 join_lines，不要改函数名与参数签名。修复后运行 python 命令验证。