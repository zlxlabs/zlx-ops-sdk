# zlx-ops-sdk

一行接入可观测的极小内部库,跨 50+ 自托管服务共用。底层用
[`sentry_sdk`](https://docs.sentry.io/platforms/python/)(GlitchTip 兼容),**不自建上报**。

它把"我手动转发告警"变成"告警带 repo/release 上下文自己路由到飞书"。

## 核心契约:fail-open by contract

> 可观测**绝不能搞死被观测者**。

跨 50 服务共用一个库,若 `init` 能抛异常,一次 GlitchTip 故障或一个坏 release 会
**同时让 50 服务开不了机**。因此:

- `init(...)` 整个包 try/except —— 缺 DSN / 坏 DSN / GlitchTip 宕 / 注册失败 / 网络超时
  一律**只记 warning,永不抛、永不阻塞应用启动或 cron**。
- **显式 timeout**:init 的网络调用有界(默认 5s),不挂起 boot。
- **kill switch**:env `ZLX_OPS_DISABLED=1` → 整个 init 直接 no-op 返回。
- 库本身**不持有任何密钥**;DSN 从 env `SENTRY_DSN` 读。

## 安装

```bash
pip install "zlx-ops-sdk~=0.1"   # 见下方"版本钉法 / rollout 规则"
```

依赖 `sentry-sdk>=2.0,<3.0`。包打了 PEP 561 `py.typed`,下游直接吃类型,无需 mypy override。

发布走 PyPI **Trusted Publishing**(OIDC,无存 token):push `v*` tag → `.github/workflows/publish.yml`
自动 build + 发布。一次性需在 pypi.org 配 pending publisher(owner/repo/workflow/environment=pypi)。

## 用法

### 1. 一行 init

放在应用入口(`main`、`app.py`、cron 脚本顶部)最早处:

```python
import zlx_ops_sdk

zlx_ops_sdk.init(
    "youtube-download-api",          # service 名(必填)
    repo="zj1123581321/youtube-download-api",
    server="n305",
    environment="prod",
    # dsn 缺省从 env SENTRY_DSN 读;release 缺省解析 git SHA
)
```

- `dsn`:缺省读 env `SENTRY_DSN`;缺失 → fail-open(只 warning,应用照常启动)。
- `release`:缺省解析为 git SHA(带 `repo@` 前缀);也可读 env `GIT_SHA` / `RELEASE`。
- 返回 `InitResult(enabled, reason)`;`enabled=False` 不是错误,只是"可观测没接上,应用照常跑"。
- `timeout`:init 网络调用上界(秒),默认 `5.0`。

未捕获异常此后自动上报到 GlitchTip,带 `service` / `repo` / `server` / `release` tag。

### 2. cron 死人开关 `@monitor`

`@monitor` 同时挂两条互补的死人开关,都 fail-open:

```python
from zlx_ops_sdk import monitor

@monitor(
    monitor_slug="nightly-sync",          # Sentry crons slug
    schedule="0 3 * * *",
    timezone="Asia/Shanghai",
    # GlitchTip 6.2 实际生效的那条:Heartbeat URL(缺省读 env ZLX_HEARTBEAT_URL)
    heartbeat_url="http://<gt>/api/0/organizations/<org>/heartbeat_check/<endpoint_id>/",
)
def nightly_sync():
    do_work()
```

#### 机制 A — GlitchTip Heartbeat URL(GlitchTip 6.2 推荐用这条)

> ⚠️ **重要**:GlitchTip 6.2 把 Sentry 的 `check_in` envelope 列入 `IgnoredItemType`
> —— 收下即丢。所以**在 GlitchTip 上真正生效的 cron 死人开关是 Heartbeat 监控**,
> 不是 `capture_checkin`。

在 GlitchTip 建一个 **Heartbeat** 类型 monitor → 拿它的 check-in URL
(`{DOMAIN}/api/0/organizations/{org}/heartbeat_check/{endpoint_id}/`),配到
`heartbeat_url` 或 env `ZLX_HEARTBEAT_URL`。语义是 **ping-on-alive**:

| 情况 | 行为 |
|---|---|
| 任务成功 | POST heartbeat URL(标记"活着") |
| 任务抛异常 | **不 ping**(异常仍向上抛),GlitchTip 因"该来没来"标记 down 告警 |
| 漏启动 | 整个进程没跑 → 自然没 ping → GlitchTip 告警(真 dead-man) |
| heartbeat 端点宕 | 只记 warning,**任务照常跑/照常抛**(fail-open) |

任务崩溃时:未捕获异常仍由 `init()` 上报成 error 事件,**且**没有 heartbeat ping
→ 双重覆盖。

#### 机制 B — Sentry crons `capture_checkin`(可移植/未来兼容,当前 GlitchTip 上 no-op)

`@monitor` 仍会发 in_progress/ok/error check-in + 用 `schedule` 注册 monitor_config。
对接**真 Sentry** 或未来支持 crons 的 GlitchTip 时这条生效;在 GlitchTip 6.2 上被忽略,
保留它零成本且向前兼容。可选参数:`schedule`(crontab)、`checkin_margin`、
`max_runtime`、`timezone`。

### 3. 常驻服务心跳 `start_heartbeat`(0.2.0+)

cron 用 `@monitor`(跑完 ping 一次);**常驻服务(daemon,无 cron、无 HTTP 端口给 Kuma/monitor 探)**
用 `start_heartbeat` —— boot 时调一次,起后台线程 **ping-on-alive**:只要 `is_alive()` 为真就周期 ping。

```python
from zlx_ops_sdk import start_heartbeat

stop = start_heartbeat(
    is_alive=lambda: consumer.is_consuming,   # 判内部链路健康(读循环活着?);线程里调,须线程安全
    url="http://<gt>/api/0/organizations/<org>/heartbeat_check/<endpoint_id>/",  # 缺省读 env ZLX_HEARTBEAT_URL
    interval_sec=90,                           # 应 < 端侧 monitor 死人窗口
)
# ... 服务运行 ...
stop()                                          # 优雅停机时停心跳(daemon 线程,进程退出也自动收)
```

| 情形 | 行为 |
|---|---|
| `is_alive()` 为真 | POST heartbeat URL(标记"内部链路活着") |
| 进程活着但内部断连(`is_alive()` 转假) | **不 ping** → 端侧超时告警(抓 monitor 探进程/端口探不到的失明) |
| `is_alive()` 自身抛 | 当作不健康跳过,心跳线程不崩 |
| ping 端点宕 / URL 未配 | 只 warning / no-op,**被观测者照常跑**(fail-open) |

为何线程而非 asyncio:任何常驻服务(同步 daemon 或 asyncio 服务)boot 时调一次即可,不绑事件循环。

## 版本钉法 / rollout 规则(爆炸半径管控)

一个 SDK 版本跨 50 服务 = 一个坏 release 同步炸 50 服务 boot。因此:

1. **钉主版本,不钉 `latest`/`@master`**:每服务 `pip install "zlx-ops-sdk~=0.1"`
   (等价 `>=0.1,<1.0`)。补丁/小版本自动收,主版本升级必须手动。
2. **canary 先升,再手动批量**:
   - 选 1 个低爆炸半径服务做 canary,先升新版本,观察 ≥24h(boot 正常 + 告警/check-in 正常路由)。
   - canary 通过后,再分批把其余服务 bump 到新版本,**不一次性全舰队升级**。
3. **主版本 bump(如 0.x → 1.0)= 破坏性变更**:走完整 canary,绝不批量直推。
4. fail-open 是兜底而非许可证:即便如此,版本钉 + canary 仍是防同步爆炸的第一道闸。

## 测试

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

覆盖:init fail-open(缺 DSN / init 抛 / 超时有界 / kill switch / happy / env DSN)、
cron Sentry-crons 四路(成功 / 失败重抛 / schedule 注册 / 端点宕 fail-open)、
cron Heartbeat(成功 ping / 失败不 ping 重抛 / 端点宕 fail-open / env URL / 未配 no-op)、
常驻心跳 start_heartbeat(健康 ping / 不健康不 ping / is_alive 抛不崩 / ping 宕 fail-open / env URL / 未配 no-op / stop 幂等)、
真实 sentry_sdk 事件集成(release+tag 实测附着)、
打包契约(py.typed 随 sdist/wheel 落地,下游免 mypy override)。

已在开发机 GlitchTip 6.2(100.87.124.57:8000)活体验证:错误事件带
`release=zj1123581321/zlx-ops-sdk@<sha>` + service/repo/server tag 入库;Heartbeat
监控收到 check(`is_up=True`)。

## 给下游(Lane C / D4 模板)的接入说明

- 包名:`zlx-ops-sdk`,import 名:`zlx_ops_sdk`,依赖钉 `~=0.1`。
- init 签名:`zlx_ops_sdk.init(service, *, dsn=None, release=None, server=None, repo=None, environment=None, timeout=5.0, **sentry_kwargs) -> InitResult`
- cron 签名:`zlx_ops_sdk.monitor(*, monitor_slug, schedule=None, checkin_margin=None, max_runtime=None, timezone=None, heartbeat_url=None, heartbeat_timeout=5.0)`
- 常驻心跳签名(0.2.0+):`zlx_ops_sdk.start_heartbeat(is_alive, url=None, *, interval_sec=60.0, timeout=5.0) -> stop()`
- env 契约:`SENTRY_DSN`(DSN 来源)、`ZLX_OPS_DISABLED=1`(kill switch)、`GIT_SHA`/`RELEASE`(release 兜底)、`ZLX_HEARTBEAT_URL`(heartbeat URL 兜底;cron 与常驻心跳共用)。
- **GlitchTip 6.2 注意**:cron 死人开关用 **Heartbeat 监控**(建 Heartbeat 类型 monitor → 配 `heartbeat_url`),Sentry `capture_checkin` 在该版本被忽略。registry 每个 cron 服务应存其 heartbeat endpoint URL。
