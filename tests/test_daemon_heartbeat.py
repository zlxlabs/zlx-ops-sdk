"""常驻服务 daemon 心跳 start_heartbeat —— ping-on-alive,fail-open。

区别于 cron @monitor(跑完 ping 一次):常驻服务起后台线程周期 ping,只在 is_alive() 为真时 ping;
内部断连(进程还活着但 is_alive 转假)→ 不 ping → 端侧超时告警。抓 monitor 探进程/端口探不到的失明。
"""
import logging
import threading
import time

import pytest

from zlx_ops_sdk import start_heartbeat

URL = "http://gt/api/0/organizations/personal/heartbeat_check/abc/"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ZLX_HEARTBEAT_URL", raising=False)


def _patch_ping(monkeypatch, *, fail=False, on_ping=None):
    pings = []

    def fake_urlopen(req, timeout=None):
        pings.append({"url": req.full_url, "method": req.get_method(), "timeout": timeout})
        if on_ping:
            on_ping()
        if fail:
            raise ConnectionError("heartbeat endpoint down")

        class _Resp:
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return pings


def test_no_url_is_noop(monkeypatch):
    pings = _patch_ping(monkeypatch)
    calls = []
    stop = start_heartbeat(lambda: calls.append(1) or True, interval_sec=0.01)
    time.sleep(0.05)
    stop()
    assert pings == []   # 没 URL 不 ping
    assert calls == []   # 线程根本没起,连 is_alive 都不调


def test_alive_pings(monkeypatch):
    got = threading.Event()
    pings = _patch_ping(monkeypatch, on_ping=got.set)
    stop = start_heartbeat(lambda: True, URL, interval_sec=0.01)
    assert got.wait(2.0), "健康服务应周期 ping"
    stop()
    assert pings[0]["url"] == URL and pings[0]["method"] == "POST"
    assert pings[0]["timeout"] is not None   # 有界,不挂起


def test_url_from_env(monkeypatch):
    monkeypatch.setenv("ZLX_HEARTBEAT_URL", URL)
    got = threading.Event()
    pings = _patch_ping(monkeypatch, on_ping=got.set)
    stop = start_heartbeat(lambda: True, interval_sec=0.01)   # 不传 url,走 env
    assert got.wait(2.0)
    stop()
    assert pings[0]["url"] == URL


def test_not_alive_skips_ping(monkeypatch):
    checked = threading.Event()
    n = []

    def alive():
        n.append(1)
        if len(n) >= 2:
            checked.set()
        return False

    pings = _patch_ping(monkeypatch)
    stop = start_heartbeat(alive, URL, interval_sec=0.01)
    assert checked.wait(2.0), "循环应在跑(is_alive 被反复调)"
    stop()
    assert pings == []   # 不健康 → 一次都没 ping(dead-man 语义)


def test_is_alive_raise_no_crash_no_ping(monkeypatch):
    checked = threading.Event()

    def alive():
        checked.set()
        raise RuntimeError("boom")

    pings = _patch_ping(monkeypatch)
    stop = start_heartbeat(alive, URL, interval_sec=0.01)
    assert checked.wait(2.0)
    time.sleep(0.03)
    stop()
    assert pings == []   # is_alive 抛 → 当作不健康,不 ping,心跳线程不崩


def test_ping_failure_is_failopen(monkeypatch, caplog):
    got = threading.Event()
    pings = _patch_ping(monkeypatch, fail=True, on_ping=got.set)
    with caplog.at_level(logging.WARNING, logger="zlx_ops_sdk"):
        stop = start_heartbeat(lambda: True, URL, interval_sec=0.01)
        assert got.wait(2.0)   # 端点宕仍尝试 ping
        stop()
    assert any("ping" in r.message.lower() for r in caplog.records)


def test_stop_halts_pings(monkeypatch):
    got = threading.Event()
    pings = _patch_ping(monkeypatch, on_ping=got.set)
    stop = start_heartbeat(lambda: True, URL, interval_sec=0.02)
    assert got.wait(2.0)
    stop()
    time.sleep(0.05)
    before = len(pings)
    time.sleep(0.1)
    assert len(pings) == before   # stop() 后不再增长
    stop()   # 幂等:再调一次也不炸
