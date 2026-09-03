"""A claimed delivery becomes a Run, as the person who sent it.

§122: a Feishu user does not become a workspace member. They are an
`EndUser`, and the path from here is the one the end-user entry already
built — `external_identities` for the mapping (§282), `caller_type=end_user`
for the Session, and the subject's own private memory. Feishu is a new
*transport* onto that path, not a second identity system.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from tiny_hermes.channels.domain.blocked import BlockedNotice, notice_from_document
from tiny_hermes.channels.domain.command_receipt import CommandReceipt
from tiny_hermes.channels.domain.commands import ChatCommand, CommandName, parse
from tiny_hermes.channels.domain.events import ChannelEvent
from tiny_hermes.channels.domain.image_reference import feishu_reference
from tiny_hermes.identity.ports.end_user_store import UpsertedIdentity
from tiny_hermes.runs.application.service import SessionBusy
from tiny_hermes.runs.domain.models import (
    EndUserEscape,
    ImageBlock,
    RunPurpose,
    SessionMode,
    SessionSnapshot,
    Withdrawal,
    WithdrawScope,
)
from tiny_hermes.runs.ports.store import AcceptedRun

#: `/undo` erases only the most recent exchange; `/new` draws a line across
#: the whole session (§8's decision that it stays one Session entity rather
#: than minting a new one). `ChatCommand.turns` only varies for `UNDO` —
#: `parse` never produces a `NEW` with anything but 1 — so `ALL` needing no
#: count of its own is `withdraw_from_session`'s concern, not this map's.
_SCOPES: dict[CommandName, WithdrawScope] = {
    CommandName.UNDO: WithdrawScope.LAST_EXCHANGE,
    CommandName.NEW: WithdrawScope.ALL,
}


class ErasedSubjectRefused(Exception):
    """§344's erasure, holding across transports.

    The web path refuses an erased subject at credential exchange
    (`EndUserIdentityService.exchange`). That check lives in the service, not
    in `upsert_external_identity`, so a second transport that forgot it
    would be a way *around* an erasure the first one honours — the subject
    would be resurrected by walking in through a different door.
    """


@dataclass(frozen=True)
class ChannelBindingRecord:
    id: UUID
    workspace_id: UUID
    agent_id: UUID
    channel: str


class SubjectDirectory(Protocol):
    """§282's upsert, exactly as the end-user entry already defines it.

    `UpsertedIdentity` is imported rather than re-declared here: a
    structurally identical copy would drift the first time that type gained
    a field, and the whole claim of this module is that Feishu is a second
    transport onto one identity path rather than a second identity system.
    """

    async def upsert_external_identity(
        self, workspace_id: UUID, channel: str, external_user_id: str
    ) -> UpsertedIdentity: ...


class Conversations(Protocol):
    async def session_for(
        self, binding_id: UUID, external_user_id: str
    ) -> UUID | None: ...

    async def remember_session(
        self, binding_id: UUID, external_user_id: str, session_id: UUID
    ) -> None: ...


class RunEntry(Protocol):
    """`create_end_user_session` and `submit_end_user_run` are the two calls
    the web entry already makes — same methods, same arguments, so Feishu
    stays a second transport rather than a second execution path.

    `withdraw_from_session` has no such precedent yet: this module is its
    first caller. It is declared here anyway, alongside the other two,
    because production wires all three to the same `RunCoordination` —
    a command's "undo" and an ordinary message's "submit" are requests to
    one Session, not to two different subsystems that happen to share a
    constructor argument.
    """

    async def create_end_user_session(
        self,
        workspace_id: UUID,
        end_user_id: UUID,
        agent_id: UUID,
        session_mode: SessionMode,
        request_id: str,
    ) -> SessionSnapshot: ...

    async def submit_end_user_run(
        self,
        workspace_id: UUID,
        end_user_id: UUID,
        session_id: UUID,
        text: str,
        idempotency_key: str | None,
        request_id: str,
        images: Sequence[ImageBlock] = (),
        purpose: RunPurpose = RunPurpose.ANSWER,
    ) -> AcceptedRun: ...

    async def request_compaction(self, session_id: UUID) -> bool:
        """记下「下一轮先压缩」，返回这段会话是否真有可压缩的历史。

        返回值由 store 给，不由调用方猜：只有它知道这段会话有多长。这一层拿它
        决定回执说哪一句——「记下了」还是「这段对话还不长」。
        """
        ...

    async def withdraw_from_session(
        self,
        session_id: UUID,
        scope: WithdrawScope,
        *,
        turns: int = 1,
        escape_hatch: EndUserEscape | None = None,
    ) -> Withdrawal | None: ...


@dataclass(frozen=True)
class Delivered:
    """What the transport has to say back.

    `blocked` is not an error and not an alternative to `run`: §497 lets the
    pending Run be saved *and* requires the caller be told. Both are present
    together, which is the shape that makes "queued, and here is why" the
    only thing a transport can express — a variant type would let one be
    handled and the other forgotten.

    `run` is `None` exactly when `receipt` is not: a command took the claim
    instead of becoming a Run, so there is nothing to attach and nothing
    that can be blocked. Every existing reader of `run` predates this and
    assumed it always present — each one has to grow a `None` branch, or a
    command reaching that code crashes on the send path instead of failing
    to reply.
    """

    run: AcceptedRun | None
    blocked: BlockedNotice | None
    receipt: CommandReceipt | None = None


@dataclass(frozen=True)
class ChannelIngestion:
    subjects: SubjectDirectory
    conversations: Conversations
    runs: RunEntry

    async def run_for(
        self,
        *,
        binding: ChannelBindingRecord,
        event: ChannelEvent,
        request_id: str,
    ) -> Delivered:
        """The claimed delivery, turned into work.

        The idempotency key is the `channel_event_id` (§569's own rule: the
        key belongs to the caller's own notion of a request). It is a second
        line rather than the first — §574's claim in `channel_events` is what
        actually stops a duplicate, and this catches the narrower case where
        a claim was taken and the Run submission was retried after a crash
        between the two.
        """
        subject = await self.subjects.upsert_external_identity(
            binding.workspace_id, binding.channel, event.external_user_id
        )
        if subject.erased_at is not None:
            raise ErasedSubjectRefused

        # A command is not a message: it changes history instead of adding
        # to it, so it takes a different path before a Session is even
        # looked up. Everything that follows this branch — session lookup,
        # creation, `submit_end_user_run` — is what makes the module
        # docstring's "not a command, whatever it starts with, reaches the
        # model untouched" claim true rather than aspirational: `parse`
        # rejecting `command` is what lets `event.text` fall through here
        # unchanged.
        command = parse(event.text, has_images=bool(event.images))
        if command is not None:
            return await self._command(
                binding, event, command, subject.end_user_id, request_id
            )

        session_id = await self.conversations.session_for(
            binding.id, event.external_user_id
        )
        if session_id is None:
            created = await self.runs.create_end_user_session(
                binding.workspace_id,
                subject.end_user_id,
                binding.agent_id,
                # Persistent, not ephemeral: a chat is a thread somebody
                # comes back to, and an ephemeral Session would discard the
                # conversation between two messages that are, to the person
                # typing them, obviously one exchange.
                SessionMode.PERSISTENT,
                request_id,
            )
            session_id = created.id
            await self.conversations.remember_session(
                binding.id, event.external_user_id, session_id
            )

        accepted = await self.runs.submit_end_user_run(
            binding.workspace_id,
            subject.end_user_id,
            session_id,
            event.text,
            event.channel_event_id,
            request_id,
            # References, not bytes. The download happens in the Worker: it
            # needs this binding's app secret, and doing it here would make an
            # inbound delivery wait on the vendor inside the webhook's own
            # response deadline.
            images=tuple(
                ImageBlock(
                    reference=feishu_reference(
                        message_id=picture.message_id, file_key=picture.file_key
                    ),
                    # Declared by the download, not guessed here. Until the
                    # bytes arrive nobody knows, and `image/*` is the honest
                    # placeholder — the Worker replaces it with what Feishu
                    # actually served.
                    media_type="image/*",
                )
                for picture in event.images
            ),
        )
        # §497: saving the pending Run is allowed, staying quiet about it is
        # not. Read here rather than left to each transport, so a transport
        # cannot forget — the notice arrives attached to the thing it
        # describes.
        return Delivered(run=accepted, blocked=notice_from_document(accepted.document))

    async def _command(
        self,
        binding: ChannelBindingRecord,
        event: ChannelEvent,
        command: ChatCommand,
        end_user_id: UUID,
        request_id: str,
    ) -> Delivered:
        """A command acts on a conversation that already exists; it does not
        start one.

        No session lookup found one here means this person has never
        exchanged a message with this binding — there is nothing to undo
        and nothing to draw a line across. Creating a Session in that case
        would turn a stranger's first-ever contact into a conversation they
        never started, purely because the words they happened to type
        collided with `/undo` or `/new`. So this returns `outcome="nothing"`
        without ever calling `create_end_user_session` or
        `remember_session` — the two calls the ordinary path above uses to
        make a conversation exist.
        """
        session_id = await self.conversations.session_for(
            binding.id, event.external_user_id
        )
        if session_id is None:
            return Delivered(
                run=None, blocked=None, receipt=_receipt(command, "nothing")
            )

        if command.name is CommandName.COMPACT:
            # 在撤回那条路之前分流：压缩不是撤回，`_SCOPES` 里没有它的条目，
            # 走下去会 KeyError。
            #
            # **这是唯一一条会产生 Run 的命令**，而这推翻了 `Delivered` 原来那条
            # 「`run` 是 `None` 恰好当 `receipt` 不是」。理由是记账：摘要是一次
            # 真实的模型调用，要花钱，而这个平台里的钱永远挂在一个 Run 上
            # （`record_summary_usage` 的 `run_id` 非空，§12.4 顺着
            # `budget_root_run_id` 累加）。当初立那条规矩的理由是「命令是纯数据
            # 操作、不花钱」——对 `/compact` 本来就不成立。
            #
            # 标记先打：Worker 在规划**之前**读走它，所以它必须在 Run 被捡起来
            # 之前就在库里。
            asked = await self.runs.request_compaction(session_id)
            if not asked:
                # 没什么可压。不建 Run——建一个只会花钱压出一份空摘要。
                return Delivered(
                    run=None, blocked=None, receipt=_receipt(command, "nothing")
                )
            accepted = await self.runs.submit_end_user_run(
                binding.workspace_id,
                end_user_id,
                session_id,
                event.text,
                # 和普通消息那条路同一个键：这条飞书消息的 id。
                # `submit_end_user_run` 第一句就要求它非空，传 `None` 会抛
                # `IdempotencyKeyRequired`——线上第一条 `/compact` 正是死在
                # 这里。而它同时是对的语义：飞书会重投同一条消息（§19.2），
                # 重投要换回同一个 Run，不是第二次花钱的压缩。
                event.channel_event_id,
                request_id,
                purpose=RunPurpose.COMPACTION,
            )
            # 回执不在这一刻发：压完之后才知道省了多少，那时才有话可说。
            return Delivered(run=accepted, blocked=None, receipt=None)

        try:
            withdrawal = await self.runs.withdraw_from_session(
                session_id,
                _SCOPES[command.name],
                turns=command.turns,
                escape_hatch=_escape_for(command, binding, end_user_id, request_id),
            )
        except SessionBusy as busy:
            return Delivered(
                run=None,
                blocked=None,
                receipt=_receipt(command, "busy", busy_reason=busy.reason),
            )
        if withdrawal is None:
            return Delivered(
                run=None, blocked=None, receipt=_receipt(command, "nothing")
            )
        return Delivered(
            run=None, blocked=None, receipt=_receipt(command, "done", withdrawal)
        )


def _escape_for(
    command: ChatCommand,
    binding: ChannelBindingRecord,
    end_user_id: UUID,
    request_id: str,
) -> EndUserEscape | None:
    """只有 `/new` 拿得到结束一个停住的队首 Run 的许可。

    `blocked_card` 对同一个人写着「被卡住时，可以发 /new 开始一段新对话」，而
    那句话指的正是队首停在等审批/等外部事件/暂停上的时候——这个许可就是让那句
    话成立的东西。`/undo` 不带它：撤回是对已经落定的历史动刀，没有理由替用户
    放弃一个他没说要放弃的 Run。
    """
    if command.name is not CommandName.NEW:
        return None
    return EndUserEscape(
        workspace_id=binding.workspace_id,
        end_user_id=end_user_id,
        request_id=request_id,
    )


def _receipt(
    command: ChatCommand,
    outcome: str,
    withdrawal: Withdrawal | None = None,
    *,
    busy_reason: str | None = None,
) -> CommandReceipt:
    return CommandReceipt(
        command=command.name.value,
        outcome=outcome,
        messages=0 if withdrawal is None else withdrawal.messages,
        turns=0 if withdrawal is None else withdrawal.turns,
        echoed_text="" if withdrawal is None else withdrawal.echoed_text,
        busy_reason=busy_reason,
        runs_ended=0 if withdrawal is None else withdrawal.runs_ended,
    )


__all__ = [
    "ChannelBindingRecord",
    "Delivered",
    "ChannelIngestion",
    "Conversations",
    "ErasedSubjectRefused",
    "RunEntry",
    "SubjectDirectory",
]
