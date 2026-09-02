app.py 目前把"配置、数据解析、报表渲染"三块职责混在一个文件里。请按目标结构拆分，同时保持对外接口完全不变：

- config.py：CONFIG 常量与 normalize_amount
- data.py：is_valid_line / parse_lines / summarize
- format.py：render_report
- app.py：只保留对外接口（App 类 + 必要的 re-export），具体实现移到上述三个模块

约束：
- from app import App 的用法必须保持可用
- 每个函数功能与现在完全一致
- 可见测试 test_app.py 不许修改且必须保持全绿
- 拆完后运行 python -m pytest -q 确认