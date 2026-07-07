"""GlitchTip Heartbeat ping 的共享底座 —— cron `@monitor` 与常驻 `start_heartbeat` 共用。

stdlib only(不引 httpx 等重依赖)。fail-open by contract:ping 失败只 warning,绝不外抛。
"""
from __future__ import annotations

import logging
import urllib.request

logger = logging.getLogger("zlx_ops_sdk")

#: heartbeat URL 的 env 兜底(单服务够用;多实例用参数显式传)。
HEARTBEAT_URL_ENV = "ZLX_HEARTBEAT_URL"
#: heartbeat ping 的网络上界(秒)。
DEFAULT_HEARTBEAT_TIMEOUT = 5.0


def ping_heartbeat(url: str, timeout: float) -> bool:
    """POST 一下 GlitchTip heartbeat URL。fail-open:失败只 warning,绝不外抛。"""
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True
    except Exception as exc:  # noqa: BLE001 — heartbeat 永不能搞死被观测者
        logger.warning(
            "zlx_ops_sdk: heartbeat ping 失败(%r),被观测者不受影响继续", exc
        )
        return False
