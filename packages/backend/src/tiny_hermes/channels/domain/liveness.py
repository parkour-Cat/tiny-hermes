"""一根长连接此刻算不算连着——判断在这里，只此一处。

`transport` 那一列说的是**配置**，不是状态：socket 死了它照样是
`long_connection`。2026-09-03 一根绑定死了十小时，控制台一直显示正常，而发现
它的方式是有人发消息发不出去。

判断放在后端而不是前端，是因为它和写心跳的那一边有个必须成立的关系
（`STALE_AFTER_SECONDS > HEARTBEAT_SECONDS`），而那两个数隔着一个 HTTP 边界
就没有任何东西能保证它们一起改。
"""

from datetime import UTC, datetime
from enum import StrEnum

#: 多久往库里写一次「我还连着」。
#:
#: 定义在 domain 而不是写它的那个适配器里，因为它和下面那个阈值之间有个必须
#: 成立的关系，而判断状态的人和写心跳的人分处两层。放在被两边都 import 的
#: 那一层，改一个就看得见另一个。
#:
#: 不跟着 `LIVENESS_POLL_SECONDS`（5 秒）走：探活是进程内读一个字段，几乎不
#: 花什么；心跳是一条 UPDATE，5 秒一次是每天一万七千次写在同一行上，全是死
#: 元组，而换来的精度没人需要——读的人问的是「还连着吗」，不是「上一秒连着
#: 吗」。60 秒是判断，不是测出来的。
HEARTBEAT_SECONDS = 60.0

#: 多久没有心跳就算断了。
#:
#: 必须大于 `HEARTBEAT_SECONDS`，否则每一拍之间都会被读成「断了」。写成乘法
#: 而不是写死一个秒数，就是让这个关系没法被单独改坏——改心跳周期，阈值跟着走。
#:
#: 三拍是判断，不是测出来的：一拍没写上可以是一次慢查询或一次 GC 停顿，连着
#: 三拍没有说明那个循环不在跑了。
STALE_AFTER_SECONDS = HEARTBEAT_SECONDS * 3


class LongConnectionState(StrEnum):
    """控制台要显示的那四种，**不是**三种。

    `NOT_APPLICABLE` 和 `NEVER` 分开：一个 webhook 绑定没有「连着」这回事，
    而一个配成长连接却从没连上过的绑定是个真问题（凭据不对、scheduler 没重启）。
    合成一种会让后者看起来正常。
    """

    NOT_APPLICABLE = "not_applicable"
    NEVER = "never"
    CONNECTED = "connected"
    STALE = "stale"


def long_connection_state(
    transport: str, seen_at: datetime | None, *, now: datetime | None = None
) -> LongConnectionState:
    """这根绑定此刻的连接状态。

    `now` 可注入，因为这是个纯函数，而测「三分钟以前的心跳算断了」不该真的
    等三分钟。

    **它只声称「写心跳的那个进程认为 socket 是通的」**，不声称对面收得到消息
    ——能证明那件事的只有真的收到一条。
    """
    if transport != "long_connection":
        return LongConnectionState.NOT_APPLICABLE
    if seen_at is None:
        return LongConnectionState.NEVER
    moment = now or datetime.now(UTC)
    # 心跳是带时区存的；一个不带时区的 `seen_at` 只可能来自手写的行，
    # 按 UTC 读比抛异常好——控制台少一格状态，不该整页打不开。
    stamped = seen_at if seen_at.tzinfo is not None else seen_at.replace(tzinfo=UTC)
    if (moment - stamped).total_seconds() <= STALE_AFTER_SECONDS:
        return LongConnectionState.CONNECTED
    return LongConnectionState.STALE
