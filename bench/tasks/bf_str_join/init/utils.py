def join_lines(lines, sep=", "):
    if not lines:
        return None  # bug 1：空列表应返回空串
    return ";".join(lines)  # bug 2：忽略了 sep 参数，固定用分号连接
