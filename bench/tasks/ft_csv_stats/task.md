stats.py 里的 column_stats(path) 还没实现，请完成它。需求：

- 用标准库 csv 读取 CSV 文件（禁止引入 pandas 等第三方库）
- 对每个"全部单元格都是数值"的列计算 mean / min / max，返回 {列名: {"mean": ..., "min": ..., "max": ...}}，数值用浮点数
- 只要某列有任何一个单元格不是数值，整列跳过（不出现在结果里）
- 文件不存在时抛 FileNotFoundError

实现后用 python 命令快速验证一下。