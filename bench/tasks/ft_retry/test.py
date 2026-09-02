"""评分断言：重试次数、间隔、异常透传、KeyboardInterrupt 不吞。"""

import time

import pytest
from retry import retry


def test_success_first_try(monkeypatch):
    sleeps = []

    @retry(max_attempts=3, backoff=0.5)
    def ok():
        return 42

    monkeypatch.setattr(time, "sleep", sleeps.append)
    assert ok() == 42
    assert sleeps == []


def test_retries_then_succeeds(monkeypatch):
    attempts = []
    sleeps = []

    @retry(max_attempts=3, backoff=0.5)
    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("还没好")
        return "done"

    monkeypatch.setattr(time, "sleep", sleeps.append)
    assert flaky() == "done"
    assert len(attempts) == 3
    assert sleeps == [0.5, 0.5]


def test_gives_up_after_max_attempts(monkeypatch):
    attempts = []

    @retry(max_attempts=2, backoff=0.1)
    def always_fails():
        attempts.append(1)
        raise RuntimeError("永远失败")

    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="永远失败"):
        always_fails()
    assert len(attempts) == 2


def test_default_arguments(monkeypatch):
    attempts = []
    sleeps = []

    @retry()
    def always_fails():
        attempts.append(1)
        raise ValueError("x")

    monkeypatch.setattr(time, "sleep", sleeps.append)
    with pytest.raises(ValueError):
        always_fails()
    assert len(attempts) == 3
    assert sleeps == [0.1, 0.1]


def test_keyboard_interrupt_not_retried():
    @retry(max_attempts=3)
    def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        interrupted()
