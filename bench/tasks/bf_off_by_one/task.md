pager.py 的 paginate(items, page, page_size) 分页结果不对，实际观察到的症状：

- paginate(list(range(10)), 1, 4) 返回 [0, 1, 2]（预期 [0, 1, 2, 3]）
- 每一页都少了最后一个元素；第 3 页 [8, 9] 只剩 [8]

修复切片边界（page 从 1 开始，最后一页不足 page_size 时返回剩余全部）。修复后运行 python 命令验证。