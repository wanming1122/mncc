geo.py 现在用 dict 表示形状、用元组表示点，在函数间传导。请重构为 dataclass：

- 新增三个 dataclass（字段名固定）：Point(x, y)、Circle(r)、Rect(w, h)，field 用 float 类型注解
- circle(r) / rect(w, h) / point(x, y) 工厂函数改为返回对应 dataclass 实例（这三个名字是外部入口，必须保留）
- area / perimeter 接受 Circle 或 Rect 实例；translate(p, dx, dy) 返回新的 Point；distance 接受两个 Point
- 数值行为与现在完全一致（圆周率用 math.pi）；函数内部不再出现 dict/元组传导

约束：可见测试 test_geo.py 不许修改且必须保持全绿（其断言兼容新旧两种实现）。重构后运行 python -m pytest -q 确认。