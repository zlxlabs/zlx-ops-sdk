"""@monitor —— cron 死人开关。

两条互补机制,都 fail-open:

1. **GlitchTip Heartbeat URL**(GlitchTip 6.2 实际生效的那条):
   POST ``{DOMAIN}/api/0/organizations/{org}/heartbeat_check/{endpoint_id}/``。
   语义 = ping-on-alive:任务成功才 ping;漏启动/崩溃则不 ping,GlitchTip 端因
   "该来没来"标记 down 告警(真正的 dead-man)。URL 走 ``heartbeat_url`` 参数或
   env ``ZLX_HEARTBEAT_URL``。

2. **Sentry crons** ``capture_checkin``(可移植/未来兼容):
   in_progress/ok/error check-in + schedule 注册。**注意 GlitchTip 6.2 把
   check_in envelope 列为 IgnoredItemType(收下即丢)**,故此条在当前 GlitchTip
   上是 no-op,保留是为对接真 Sentry 及未来支持 crons 的 GlitchTip。

不自建上报。check-in / heartbeat 失败一律只记 warning,**任务照常跑/照常抛**。
"""
from __future__ import annotations

import functools
import logging
import os
import time
from typing import Any, Callable, Optional

# ping 底座在 _ping(与常驻 start_heartbeat 共用);别名保留原私有名不破坏引用。
from ._ping import DEFAULT_HEARTBEAT_TIMEOUT, HEARTBEAT_URL_ENV
from ._ping import ping_heartbeat as _ping_heartbeat

logger = logging.getLogger("zlx_ops_sdk")

__all__ = ["monitor", "HEARTBEAT_URL_ENV", "DEFAULT_HEARTBEAT_TIMEOUT"]


def _build_monitor_config(schedule: Optional[str], **extra) -> Optional[dict]:
    """把 crontab 字符串 + 可选项组装成 GlitchTip monitor_config。"""
    if schedule is None:
        return None
    cfg: dict[str, Any] = {"schedule": {"type": "crontab", "value": schedule}}
    cfg.update({k: v for k, v in extra.items() if v is not None})
    return cfg


def _safe_checkin(**kwargs) -> Optional[str]:
    """capture_checkin 的 fail-open 包装。端点宕只记 warning,绝不外抛。"""
    try:
        from sentry_sdk.crons import capture_checkin

        return capture_checkin(**kwargs)
    except Exception as exc:  # noqa: BLE001 — check-in 永不能搞死任务
        logger.warning(
            "zlx_ops_sdk: cron check-in 失败(%r),任务不受影响继续", exc
        )
        return None


def monitor(
    *,
    monitor_slug: str,
    schedule: Optional[str] = None,
    checkin_margin: Optional[int] = None,
    max_runtime: Optional[int] = None,
    timezone: Optional[str] = None,
    heartbeat_url: Optional[str] = None,
    heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT,
) -> Callable:
    """装饰一个 cron 任务,自动做死人开关 check-in。

    :param monitor_slug: Sentry crons 的 monitor slug。
    :param schedule: crontab 表达式;传了才注册 schedule、Sentry 端才能标记漏启动。
    :param checkin_margin: 允许迟到的分钟数(Sentry crons)。
    :param max_runtime: 最大运行分钟数(Sentry crons)。
    :param timezone: schedule 的时区(Sentry crons)。
    :param heartbeat_url: GlitchTip Heartbeat URL;缺省读 env ``ZLX_HEARTBEAT_URL``。
        任务成功才 ping;失败/漏启动不 ping → GlitchTip 端告警(GlitchTip 6.2 生效路径)。
    :param heartbeat_timeout: heartbeat ping 的网络上界(秒)。
    """
    monitor_config = _build_monitor_config(
        schedule,
        checkin_margin=checkin_margin,
        max_runtime=max_runtime,
        timezone=timezone,
    )

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            check_in_id = _safe_checkin(
                monitor_slug=monitor_slug,
                status="in_progress",
                monitor_config=monitor_config,
            )
            started = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except BaseException:
                _safe_checkin(
                    monitor_slug=monitor_slug,
                    status="error",
                    check_in_id=check_in_id,
                    duration=time.monotonic() - started,
                    monitor_config=monitor_config,
                )
                # heartbeat 是 ping-on-alive:失败不 ping,让 GlitchTip 因漏到告警
                raise  # 异常仍向上抛,不吞
            _safe_checkin(
                monitor_slug=monitor_slug,
                status="ok",
                check_in_id=check_in_id,
                duration=time.monotonic() - started,
                monitor_config=monitor_config,
            )
            url = heartbeat_url or os.environ.get(HEARTBEAT_URL_ENV)
            if url:
                _ping_heartbeat(url, heartbeat_timeout)
            return result

        return wrapper

    return decorator
