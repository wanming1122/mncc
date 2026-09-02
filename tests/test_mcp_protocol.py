"""MCP 帧编解码（M6 D1）：encode/decode 往返、跨 chunk 重组、非法帧、EOF。"""

from __future__ import annotations

import io

import pytest

from mncc.mcp.protocol import (
    ProtocolError,
    decode_message,
    encode_message,
    make_notification,
    make_request,
)


def _reader(data: bytes) -> io.BufferedReader:
    return io.BufferedReader(io.BytesIO(data))


class TestEncodeMessage:
    def test_ascii_frame_format(self) -> None:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        frame = encode_message(payload)
        header, _, body = frame.partition(b"\r\n\r\n")
        assert header.startswith(b"Content-Length: ")
        assert len(body) == int(header.split(b": ", 1)[1])

    def test_utf8_length_counts_bytes_not_chars(self) -> None:
        # "你好" 各 3 字节；长度头必须按字节数而非字符数
        frame = encode_message({"text": "你好"})
        header, _, body = frame.partition(b"\r\n\r\n")
        # {"text": "你好"}：ascii 部分 12 字节 + 中文 6 字节 = 18；字符数只有 12
        assert len(body) == 18
        assert int(header.split(b": ", 1)[1]) == len(body) == 18


class TestDecodeMessage:
    def test_roundtrip(self) -> None:
        payload = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        assert decode_message(_reader(encode_message(payload))) == payload

    def test_utf8_roundtrip(self) -> None:
        payload = {"text": "你好，世界"}
        assert decode_message(_reader(encode_message(payload))) == payload

    def test_multiple_frames_in_stream(self) -> None:
        a = {"id": 1, "result": 1}
        b = {"id": 2, "result": 2}
        stream = _reader(encode_message(a) + encode_message(b))
        assert decode_message(stream) == a
        assert decode_message(stream) == b
        assert decode_message(stream) is None

    def test_cross_chunk_reassembly(self) -> None:
        # 模拟网络分块：帧被切成 7 字节小块，decode 必须读满 Content-Length
        payload = {"result": "你好世界" * 50}
        frame = encode_message(payload)
        chunks = [frame[i : i + 7] for i in range(0, len(frame), 7)]
        buf = io.BufferedReader(io.BytesIO(b"".join(chunks)))
        assert decode_message(buf) == payload

    def test_eof_before_any_header_returns_none(self) -> None:
        assert decode_message(_reader(b"")) is None

    def test_missing_content_length_raises(self) -> None:
        with pytest.raises(ProtocolError, match="Content-Length"):
            decode_message(_reader(b"Foo: bar\r\n\r\n{}"))

    def test_invalid_length_raises(self) -> None:
        with pytest.raises(ProtocolError, match="Content-Length"):
            decode_message(_reader(b"Content-Length: abc\r\n\r\n{}"))

    def test_truncated_body_raises(self) -> None:
        frame = encode_message({"result": "data"})
        with pytest.raises(ProtocolError, match="正文"):
            decode_message(_reader(frame[:-5]))

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ProtocolError, match="JSON"):
            decode_message(_reader(b"Content-Length: 3\r\n\r\nnot"))

    def test_eof_mid_header_raises(self) -> None:
        with pytest.raises(ProtocolError, match="头部"):
            decode_message(_reader(b"Content-Length: 5\r\n"))


class TestMakeMessages:
    def test_request_has_id(self) -> None:
        assert make_request("tools/call", {"name": "x"}, 3) == {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "x"},
        }

    def test_notification_has_no_id(self) -> None:
        notification = make_notification("notifications/initialized", {})
        assert "id" not in notification
        assert notification["method"] == "notifications/initialized"
