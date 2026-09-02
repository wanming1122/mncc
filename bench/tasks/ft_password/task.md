实现 password.py 里的 strength(password) 函数，规则：

- 长度 < 8：weak（无论内容）
- 长度 >= 8 时按包含的字符类别加分，四类：小写字母、大写字母、数字、符号（非字母非数字）
  - 类别数 <= 2：weak
  - 类别数 == 3：medium
  - 类别数 == 4 且长度 >= 8：strong

空字符串按 weak。用标准库实现，改完用 python 命令验证几个例子。