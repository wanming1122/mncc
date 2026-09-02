def paginate(items, page, page_size):
    """返回第 page 页（从 1 开始）的元素列表。"""
    start = (page - 1) * page_size
    end = start + page_size - 1  # bug：end 少了 1，每页都丢最后一个元素
    return items[start:end]
