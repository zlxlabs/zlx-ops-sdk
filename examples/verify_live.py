"""真实服务接入活体验证 —— 把一个真事件 + 一次 cron check-in 送进 GlitchTip。

用法(在能访问 GlitchTip 的机器上):

    export SENTRY_DSN='https://<key>@<host>:8000/<project_id>'
    export ZLX_VERIFY_SERVER='<host>'
    python examples/verify_live.py

预期:
  1. GlitchTip 该 project 收到一条 RuntimeError 事件,带
     service / repo / server tag + release(含 repo 前缀的 git SHA)。
  2. 对应 monitor_slug 收到一次 in_progress + 一次 ok check-in。
若没配 DSN,脚本走 fail-open 路径(只 warning,不报错)—— 这本身也验证了契约。
"""
import logging
import os

logging.basicConfig(level=logging.INFO)

import zlx_ops_sdk
from zlx_ops_sdk import monitor

SERVICE = os.environ.get("ZLX_VERIFY_SERVICE", "zlx-ops-sdk-selfcheck")
SLUG = os.environ.get("ZLX_VERIFY_SLUG", "zlx-ops-sdk-selfcheck-cron")


def main() -> None:
    res = zlx_ops_sdk.init(
        SERVICE,
        repo="zlxlabs/zlx-ops-sdk",
        server=os.environ["ZLX_VERIFY_SERVER"],
        environment="verify",
    )
    print(f"init -> enabled={res.enabled} reason={res.reason}")

    import sentry_sdk

    # 1) 真异常事件
    try:
        raise RuntimeError("zlx-ops-sdk live verify: 这是一条预期内的测试告警")
    except RuntimeError:
        event_id = sentry_sdk.capture_exception()
    print(f"captured exception event_id={event_id}")

    # 2) cron 死人开关(成功路)。GlitchTip 6.2 走 Heartbeat URL;
    #    env ZLX_HEARTBEAT_URL 配上 Heartbeat monitor 的 check-in URL 才会真打点。
    @monitor(monitor_slug=SLUG, schedule="*/5 * * * *", timezone="Asia/Shanghai")
    def sample_cron():
        print("cron 任务体执行中...")
        return "done"

    print(f"cron -> {sample_cron()}")
    if not os.environ.get("ZLX_HEARTBEAT_URL"):
        print("提示:未设 ZLX_HEARTBEAT_URL → GlitchTip 6.2 上 cron 不会真打点"
              "(capture_checkin 被忽略);建 Heartbeat monitor 后配该 env 再跑。")

    sentry_sdk.flush(timeout=5)
    print("flush 完成。去 GlitchTip 看事件 + heartbeat check。")


if __name__ == "__main__":
    main()
