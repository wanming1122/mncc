在当前目录新建命令行 TODO 应用 todo.py（argparse + json 文件存储），用法：

    python todo.py add <内容>     新增，打印 "added: <id>: <内容>"
    python todo.py list           列出未完成项，按 id 升序，每行 "<id>: <内容>"；没有则打印 "empty"
    python todo.py done <id>      标记完成，打印 "done: <id>"；id 不存在打印 "not found: <id>"
    python todo.py remove <id>    删除，打印 "removed: <id>"；id 不存在打印 "not found: <id>"

- 数据存当前目录 todos.json：列表，每项 {"id": 整数, "text": 字符串, "done": 布尔}；不存在时自动创建
- id 从 1 开始自增，删除/完成后不复用
- 非法子命令或参数错误时 argparse 打印 usage 并以退出码 2 退出

零第三方库。实现后运行几条命令自查。