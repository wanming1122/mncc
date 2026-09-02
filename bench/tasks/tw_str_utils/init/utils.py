"""字符串工具函数（本文件实现正确，任务是为它写测试，不要修改本文件）。"""


def reverse_words(text):
    """按空白切分后反转单词顺序，单词间用单个空格连接；空串返回空串。"""
    if not text.strip():
        return ""
    return " ".join(reversed(text.split()))


def title_case(text):
    """每个单词首字母大写、其余小写；空串返回空串。"""
    if not text:
        return ""
    return " ".join(w.capitalize() for w in text.split())


def count_vowels(text):
    """统计 aeiou 元音个数（不区分大小写）。"""
    return sum(1 for c in text.lower() if c in "aeiou")
