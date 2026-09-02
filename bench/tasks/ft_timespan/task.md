实现 timespan.py 的 ISO 8601 duration 子集（只有到小时级，无天以上单位）：

- parse_duration("PT1H30M") -> 5400（返回秒数）；H/M/S 任意组合、值为 0 的单位可省略：
  PT30M -> 1800，PT45S -> 45，PT1H -> 3600，PT1H15S -> 3615
- 非法输入抛 ValueError：不是 P 开头、没有 T、单位前缺数字、同一单位重复、含负号或小数
- format_duration(seconds) -> 反向：5400 -> "PT1H30M"；不足 1 小时的省略 H：
  format_duration(1800) -> "PT30M"，75 -> "PT1M15S"，3600 -> "PT1H"，0 -> "PT0S"
  值为 0 的单位一律省略（除了全部为 0 时输出 "PT0S"）

两者互逆：parse_duration(format_duration(s)) == s。用标准库实现，改完用 python 命令验证。