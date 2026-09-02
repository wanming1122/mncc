"""共享 fixtures：输出重定向到 StringIO、隔离 MNCC 相关环境变量。"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from mncc.config import API_KEY_ENV_FALLBACKS

_ALL_ENV = (*API_KEY_ENV_FALLBACKS, "MY_KEY", "MNCC_BASE_URL", "MNCC_MODEL")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试不受本机真实环境变量影响（key、base_url、model 一律从零开始）。"""
    for name in _ALL_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def string_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, force_terminal=False, width=100), buf
