"""控制台那一格该显示什么。"""

from datetime import UTC, datetime, timedelta

from tiny_hermes.channels.domain.liveness import (
    STALE_AFTER_SECONDS,
    LongConnectionState,
    long_connection_state,
)
from tiny_hermes.channels.infrastructure.feishu_long_connection import HEARTBEAT_SECONDS

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def test_the_stale_threshold_leaves_room_for_a_missed_beat() -> None:
    """阈值必须大于心跳周期，否则每一拍之间都会被读成「断了」。

    两个数分属两个模块（判断在 domain，写心跳在 infrastructure），改一个忘了
    另一个的后果不是报错，是控制台稳定地说假话——所以这里把关系本身钉住，
    而不是钉住 180 这个值。
    """
    assert STALE_AFTER_SECONDS > HEARTBEAT_SECONDS


def test_a_webhook_binding_has_no_connection_state() -> None:
    """webhook 是别人打进来的，没有「连着」这回事。"""
    assert (
        long_connection_state("webhook", None, now=NOW)
        is LongConnectionState.NOT_APPLICABLE
    )


def test_a_long_connection_that_was_never_seen_is_not_the_same_as_a_webhook() -> None:
    """配成长连接却从没连上过，是个真问题——凭据不对，或者 scheduler 没重启。

    和 `NOT_APPLICABLE` 合成一种会让它看起来正常，而它正是最需要被看见的那种。
    """
    assert long_connection_state("long_connection", None, now=NOW) is LongConnectionState.NEVER


def test_a_recent_heartbeat_reads_as_connected() -> None:
    seen = NOW - timedelta(seconds=HEARTBEAT_SECONDS)
    assert (
        long_connection_state("long_connection", seen, now=NOW)
        is LongConnectionState.CONNECTED
    )


def test_a_heartbeat_older_than_the_threshold_reads_as_stale() -> None:
    """这一条就是 2026-09-03 那十个小时。

    那时 socket 早就死了，而控制台显示的还是「长连接」——因为那一列说的是
    存的值。这条测试钉的是「不再显示成连着」。
    """
    seen = NOW - timedelta(seconds=STALE_AFTER_SECONDS + 1)
    assert (
        long_connection_state("long_connection", seen, now=NOW) is LongConnectionState.STALE
    )


def test_a_naive_timestamp_does_not_take_the_page_down() -> None:
    """手写的行可能没带时区。少一格状态可以，整页打不开不行。"""
    seen = (NOW - timedelta(seconds=1)).replace(tzinfo=None)
    assert (
        long_connection_state("long_connection", seen, now=NOW)
        is LongConnectionState.CONNECTED
    )
