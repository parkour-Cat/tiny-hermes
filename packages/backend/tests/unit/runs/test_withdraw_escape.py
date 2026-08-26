"""`/new` 取消不掉那个停住的 Run 时会发生什么。

这一条不能在集成里造：取消一个停住的队首在真库里总是合法的（状态机三个停住
的状态到 `cancelled` 都有边），所以「取消失败」只能在这一层用一个假的 store
钉住。它值得被钉住的原因是本项目最常见的那种 bug 的反面——**如果失败了还照样
撤，用户拿到的是一段新对话加上一个还能醒进来的旧 Run**，而这个分裂正是撤回这
个功能要消除的东西。

断言两件事，缺一不可：抛出的是忙，以及 `mark_withdrawn` **一次都没被调用**。
只看异常的测试抓不到「拒绝了但顺手改了几行」的实现。
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from tiny_hermes.runs.application.service import RunCoordination, SessionBusy
from tiny_hermes.runs.domain.models import (
    CallerIdentity,
    CallerType,
    EndUserEscape,
    SessionMode,
    SessionSnapshot,
    StoppedRun,
    UnfinishedWork,
    WithdrawScope,
)
from tiny_hermes.runs.ports.store import ControlRunCommand

WORKSPACE = uuid4()
END_USER = uuid4()
SESSION = uuid4()
PARKED_RUN = uuid4()
QUEUED_BEHIND = uuid4()


class RefusingStore:
    """只实现 `withdraw_from_session` 这条路会碰到的方法。

    `control_run` 抛 `DeniedRunControl` —— `SqlRunStore.control_run` 在状态机
    拒绝一次转移时抛的就是它。
    """

    def __init__(self, failure: Exception, fail_on: UUID = PARKED_RUN) -> None:
        self.failure = failure
        #: 哪一个 Run 的取消会失败。默认队首，`fail_on=QUEUED_BEHIND` 用来造
        #: 「前面几个已经取消掉了，后面这个失败」这种半路失败。
        self.fail_on = fail_on
        self.cancelled: list[UUID] = []
        self.withdrawn: list[Sequence[UUID]] = []

    async def unfinished_work(self, session_id: UUID) -> UnfinishedWork:
        del session_id
        # 排队的在前，停住的队首在最后——`unfinished_work` 交出来的顺序。
        return UnfinishedWork(
            reason="parked",
            cancellable=(
                StoppedRun(run_id=QUEUED_BEHIND, state_version=1),
                StoppedRun(run_id=PARKED_RUN, state_version=3),
            ),
        )

    async def get_run(
        self, workspace_id: UUID, run_id: UUID, capabilities: Any
    ) -> Any:
        del workspace_id, capabilities
        # `get_end_user_run` 只读 `run.session_id`，用它去问归属。
        return type("Stub", (), {"session_id": SESSION, "id": run_id})()

    async def get_session(
        self, workspace_id: UUID, session_id: UUID
    ) -> SessionSnapshot:
        return SessionSnapshot(
            id=session_id,
            workspace_id=workspace_id,
            agent_id=uuid4(),
            session_mode=SessionMode.PERSISTENT,
            caller=CallerIdentity(CallerType.END_USER, END_USER),
            head_run_id=PARKED_RUN,
            next_run_sequence=2,
            next_message_sequence=2,
            workspace_revision_id=None,
            created_at=datetime.now(UTC),
        )

    async def control_run(self, command: ControlRunCommand) -> Any:
        if command.run_id == self.fail_on:
            raise self.failure
        self.cancelled.append(command.run_id)
        return None

    async def withdrawable(
        self, session_id: UUID, scope: WithdrawScope, turns: int
    ) -> tuple[list[UUID], int, str]:
        del session_id, scope, turns
        return [uuid4()], 1, "帮我查一下上周的订单"

    async def mark_withdrawn(
        self, message_ids: Sequence[UUID], *, at: datetime
    ) -> int:
        del at
        self.withdrawn.append(message_ids)
        return len(message_ids)


async def test_a_cancel_that_failed_refuses_instead_of_withdrawing_anyway() -> None:
    from tiny_hermes.runs.application.service import DeniedRunControl

    store = RefusingStore(
        DeniedRunControl("invalid_state_transition"), fail_on=QUEUED_BEHIND
    )
    coordination = RunCoordination(store)  # pyright: ignore[reportArgumentType]

    with pytest.raises(SessionBusy) as raised:
        await coordination.withdraw_from_session(
            SESSION,
            WithdrawScope.ALL,
            escape_hatch=EndUserEscape(
                workspace_id=WORKSPACE, end_user_id=END_USER, request_id="req-new"
            ),
        )

    assert raised.value.reason == "cancel_failed"
    assert store.cancelled == []
    assert store.withdrawn == []


async def test_a_failure_half_way_through_still_withdraws_nothing() -> None:
    """`/new` 要结束的是这个 Session 的**全部**未了结工作。第一个取消成功、
    第二个失败时，撤回照做等于既撤了历史又留下一个还会答复的 Run —— 比拒绝糟。

    这里断言的是可观察的那一半：一行都没撤。已经发出去的那次取消不会被回滚，
    这一层没有 savepoint，`unfinished_work` 的事前合法性检查是挡这件事的地方，
    不是这里 —— 别把这条测试读成这里有回滚。
    """
    from tiny_hermes.runs.application.service import DeniedRunControl

    store = RefusingStore(
        DeniedRunControl("invalid_state_transition"), fail_on=PARKED_RUN
    )
    coordination = RunCoordination(store)  # pyright: ignore[reportArgumentType]

    with pytest.raises(SessionBusy) as raised:
        await coordination.withdraw_from_session(
            SESSION,
            WithdrawScope.ALL,
            escape_hatch=EndUserEscape(
                workspace_id=WORKSPACE, end_user_id=END_USER, request_id="req-new"
            ),
        )

    assert raised.value.reason == "cancel_failed"
    assert store.cancelled == [QUEUED_BEHIND]
    assert store.withdrawn == []


async def test_a_state_version_conflict_refuses_the_same_way() -> None:
    """取消失败有两种：非法转移，和状态版本被别人抢先动过。两种的后果一样
    ——那个 Run 还在——所以给用户的回答也必须一样。
    """
    from tiny_hermes.runs.application.service import StateVersionConflict

    store = RefusingStore(StateVersionConflict(), fail_on=QUEUED_BEHIND)
    coordination = RunCoordination(store)  # pyright: ignore[reportArgumentType]

    with pytest.raises(SessionBusy) as raised:
        await coordination.withdraw_from_session(
            SESSION,
            WithdrawScope.ALL,
            escape_hatch=EndUserEscape(
                workspace_id=WORKSPACE, end_user_id=END_USER, request_id="req-new"
            ),
        )

    assert raised.value.reason == "cancel_failed"
    assert store.withdrawn == []
