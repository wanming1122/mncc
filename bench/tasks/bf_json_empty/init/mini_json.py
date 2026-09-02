"""极简 JSON 解析器：支持 // 与 # 行注释（教学用）。"""

import json


def _strip_comments(text):
    out = []
    for line in text.splitlines():
        line = line.rsplit("//", 1)[0].strip()
        if line.startswith("#"):
            line = ""
        out.append(line)
    return "\n".join(out)


def loads(text):
    text = _strip_comments(text)
    return json.loads(text)  # bug：空串/纯注释输入抛裸 json.JSONDecodeError


class MiniJsonError(ValueError):
    """面向用户的解析错误。"""
