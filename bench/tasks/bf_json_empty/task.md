mini_json.py 的 loads(text) 先剥掉 // 与 # 注释再解析 JSON。当前有个报错问题：

- loads("") 与 loads("// 只有注释") 这类输入，会抛出 json 模块的裸 JSONDecodeError
- 期望：这类输入抛出 MiniJsonError（ValueError 子类，mini_json.py 中已有定义），并带一句人能看懂的原因

正常 JSON 的解析行为要完全保持不变。修复后运行 python 命令验证几种输入。