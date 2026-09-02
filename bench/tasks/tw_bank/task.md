请为 bank.py 的 Account 类编写 pytest 测试，写到 test_bank.py。行为规约：

- 新账户余额为 0（Account.balance 是只读属性）
- deposit(amount) 增加余额；amount <= 0 抛 ValueError（存款后余额是 amount）
- withdraw(amount) 减少余额；amount <= 0 或 amount > 余额时抛 ValueError，且余额不变
- 连续存取后余额合计正确；恰好取出全部余额是被允许的

要求：
- 覆盖边界：0、负数、恰好等于余额、超余额 1 元等
- 测试要能发现"实现悄悄变坏"：如果实现出现一处行为错误，你的测试必须至少有一个失败
- 不要修改 bank.py；写完运行 pytest test_bank.py 确认全绿