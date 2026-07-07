"""常驻服务(daemon)ping-on-alive 心跳 —— 舰队"无 HTTP 端口给 Kuma 探"的常驻服务范式。

区别于 cron 的 ``@monitor``(任务跑完 ping 一次):常驻服务 boot 时调一次 ``start_heartbeat``,
起一个 daemon 线程,只要 ``is_alive()`` 为真(读循环/内部链路健康)就每 ``interval_sec`` POST 一次
heartbeat URL;内部断连/死锁但进程还活着时 ``is_alive()`` 转假 → 不 ping → GlitchTip 端因超时告警。
这抓的是"进程活着但内部链路断"这类**死人开关(monitor 探进程/端口)探不到的失明**。

用线程而非 asyncio:任何常驻服务(同步 daemon 或 asyncio 服务)boot 时调一次即可,不绑事件循环。
``is_alive`` 会在心跳线程里被调用,须线程安全(读一个 bool / 时间戳即可)。

fail-open by contract:URL 未配 → no-op;ping 失败吞掉(真死靠端侧超时判);``is_alive()`` 自身抛
→ 当作不健康跳过。绝不搞死被观测者。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Optional

from ._ping import DEFAULT_HEARTBEAT_TIMEOUT, HEARTBEAT_URL_ENV, ping_heartbeat

logger = logging.getLogger("zlx_ops_sdk")


def start_heartbeat(
    is_alive: Callable[[], bool],
    url: Optional[str] = None,
    *,
    interval_sec: float = 60.0,
    timeout: float = DEFAULT_HEARTBEAT_TIMEOUT,
) -> Callable[[], None]:
    """起常驻服务心跳,返回 ``stop()``(幂等、随时可调)。

    :param is_alive: 判服务内部是否健康的 callable(心跳线程里调,须线程安全)。
        返回 True 才 ping;False/抛异常都跳过 ping,让端侧超时告警。
    :param url: GlitchTip Heartbeat URL;缺省读 env ``ZLX_HEARTBEAT_URL``。未配 → no-op。
    :param interval_sec: ping 周期(秒),应 < 端侧 monitor 的死人窗口(interval)。
    :param timeout: 单次 ping 的网络上界(秒)。
    :returns: ``stop()`` —— 调用后停心跳线程(daemon 线程,进程退出也会自动收)。
    """
    resolved = url or os.environ.get(HEARTBEAT_URL_ENV)
    stop_event = threading.Event()

    def _stop() -> None:
        stop_event.set()

    if not resolved:
        logger.info(
            "zlx_ops_sdk: 未配 heartbeat URL(%s),常驻心跳 no-op", HEARTBEAT_URL_ENV
        )
        return _stop

    def _loop() -> None:
        # 先等一个 interval 再 ping(与 cron 语义一致:刚启动那一刻不算一次心跳)。
        while not stop_event.wait(interval_sec):
            try:
                alive = is_alive()
            except Exception as exc:  # noqa: BLE001 — is_alive 抛不能搞死心跳
                logger.warning(
                    "zlx_ops_sdk: heartbeat is_alive() 抛(%r),视为不健康跳过 ping", exc
                )
                alive = False
            if not alive:
                logger.warning(
                    "zlx_ops_sdk: 服务不健康,跳过 heartbeat ping(→端侧将超时告警)"
                )
                continue
            ping_heartbeat(resolved, timeout)  # 已 fail-open

    thread = threading.Thread(target=_loop, name="zlx-ops-heartbeat", daemon=True)
    thread.start()
    return _stop
