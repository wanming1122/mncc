请为 series.py 的两个函数编写 pytest 测试，写到 test_series.py。行为规约：

- arithmetic_sum(n)：1+2+...+n；n=0 返回 0；n=1 返回 1；负数抛 ValueError；较大 n（如 100）结果正确
- geometric_sum(a, r, n)：a + a*r + ... + a*r**(n-1)
  - n=0 返回 0.0；n 为负抛 ValueError
  - r=1 特例：结果为 a*n（实现里这是除零保护分支）
  - r 非 1 的小数近似用 pytest.approx 断言
  - a 可以取负值与 0

要求：
- 覆盖边界与特例；测试要能发现"实现悄悄变坏"——如果实现出现一处行为错误，你的测试必须至少有一个失败
- 不要修改 series.py；写完运行 pytest test_series.py 确认全绿