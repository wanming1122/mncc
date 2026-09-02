calc.py 里 sub 与 div 的行为不对，实际观察到的症状：

- sub(5, 3) 返回 7（预期 2）
- div(1, 0) 返回 0 而不是抛出 ZeroDivisionError

请找到问题并把 calc.py 修好，然后运行 python 命令验证修复后的行为符合预期。