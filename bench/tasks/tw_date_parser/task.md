请为 date_parser.py 的 parse_date 编写 pytest 测试，写到 test_date_parser.py。行为规约：

- 解析 "YYYY-MM-DD" 返回 datetime.date；str(结果) 能还原成原字符串
- 闰年：2024-02-29 有效；2023-02-29 抛 ValueError
- 非法输入抛 ValueError：格式错误（长度不为 10、分隔符缺失、含非数字）、月/日越界（第 13 月、2 月 30 日等）

要求：
- 覆盖边界与非法输入；测试要能发现"实现悄悄变坏"——如果实现出现一处行为错误，你的测试必须至少有一个失败
- 不要修改 date_parser.py；写完运行 pytest test_date_parser.py 确认全绿