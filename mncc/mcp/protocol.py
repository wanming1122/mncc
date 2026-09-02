"""MCP over stdio 的 JSON-RPC 帧编解码（M6 D1，手写最小子集）。

framing 为什么是 `Content-Length: N\r\n\r\n<body>`：JSON 正文本身可以包含
`\r\n`（字符串里的换行），按行分隔不可靠；必须显式给出按**字节数**计算的
长度（UTF-8 中一个中文是 3 字节，不能用字符数）。解码时同样要"读满 N 字节"，
即使 body 跨了多个 read() 分块也要重组完整——这就是面试点里的跨 chunk 重组。
"""

from __future__ import annotations

import json
from io import BufferedReader

_HEADER_END = b"\r\n\r\n"
_HEADER_ENCODING = "ascii"
# 防御：拒绝超大帧，避免恶意/损坏的头部把我们带进 OOM
_MAX_FRAME_BYTES = 16 * 1024 * 1024


class ProtocolError(Exception):
    """帧格式非法（头部缺失/长度非法/JSON 解析失败）。"""


def encode_message(payload: dict) -> bytes:
    """JSON-RPC over stdio 帧编码：json.dumps + Content-Length 头。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode(_HEADER_ENCODING) + _HEADER_END + body


def decode_message(stream: BufferedReader) -> dict | None:
    """读一帧：解析 Content-Length 头，读满 body，返回 dict；EOF 返回 None。

    实现要点：
    - 按行读头部直到空行（\r\n\r\n），兼容仅 \n 的宽松头；
    - 长度按字节数；body 可能跨多次 read()，循环读满 N 字节；
    - EOF 发生在头部第一行之前 → 返回 None（对端干净关闭）；
    - EOF 发生在读到一半 → ProtocolError（对端异常中断，帧不完整）。
    """
    headers: dict[str, str] = {}
    while True:
        line = _readline(stream)
        if line is None:  # 对端关闭
            if not headers:
                return None
            raise ProtocolError("对端在帧头部中途关闭了连接")
        if not line:  # 空行 = 头部结束（\r\n 已被 _readline 剥掉）
            break
        key, sep, value = line.decode(_HEADER_ENCODING, "replace").partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
        # 忽略不认识的头部（MCP 标准如此：Content-Type 等）

    length = headers.get("content-length")
    if length is None or not length.isdigit():
        raise ProtocolError(f"缺少合法 Content-Length 头：{headers!r}")
    size = int(length)
    if size < 0 or size > _MAX_FRAME_BYTES:
        raise ProtocolError(f"Content-Length 超出范围：{size}")

    body = _read_exact(stream, size)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"帧正文不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError(f"帧正文必须是 JSON 对象，收到 {type(payload).__name__}")
    return payload


def make_request(method: str, params: dict, _id: int) -> dict:
    """构造 JSON-RPC 请求（带 id，必须有响应）。"""
    return {"jsonrpc": "2.0", "id": _id, "method": method, "params": params}


def make_notification(method: str, params: dict) -> dict:
    """构造 JSON-RPC 通知（无 id，对端不应回复）。"""
    return {"jsonrpc": "2.0", "method": method, "params": params}


def _readline(stream: BufferedReader) -> bytes | None:
    """读一行（到 \n 为止），剥掉行尾 \r\n。EOF 时返回 None。"""
    chunks: list[bytes] = []
    while True:
        byte = stream.read(1)
        if not byte:
            if not chunks:
                return None
            break
        chunks.append(byte)
        if byte == b"\n":
            break
        if len(chunks) > _MAX_FRAME_BYTES:
            raise ProtocolError("帧头过长")
    line = b"".join(chunks)
    if line.endswith(b"\n"):
        line = line[:-1]
        if line.endswith(b"\r"):
            line = line[:-1]
    return line


def _read_exact(stream: BufferedReader, size: int) -> bytes:
    """循环读满 size 字节（跨 read() 分块重组）。"""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise ProtocolError(f"对端在帧正文中途关闭了连接（差 {remaining} 字节）")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
