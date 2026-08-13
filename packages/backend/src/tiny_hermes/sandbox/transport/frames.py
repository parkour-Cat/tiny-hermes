"""The framed streaming subprotocol for the controller socket.

The existing one-JSON-line-per-call protocol stays for small control calls;
workspace bytes and long command output move through these frames instead
(design §5.3). This module is deliberately socket-free: a pure codec and a
receiver state machine, for the same reason ``container_policy.py`` is pure —
the rules must be testable on Windows, where the Unix socket is not.

Wire format per frame: 4-byte big-endian payload length, 1 type byte, 8-byte
big-endian sequence, payload. The length is checked against the frame cap
*before* any buffering decision, so a hostile length field cannot make the
receiver allocate its way into an OOM kill.

The receiver never reads a clock. It is told the time of each frame and
answers ``next_deadline()`` — a decision the transport enforces, not a sleep
the state machine performs. That is what makes the 30-second idle rule a unit
test instead of a 30-second test.
"""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

#: One frame's payload ceiling. Large enough that a 2 GiB workspace is ~2048
#: frames, small enough that a single frame cannot monopolize the socket.
MAX_FRAME_PAYLOAD = 1_048_576
#: Unacknowledged bytes the sender may have in flight before it must wait.
RECEIVE_CREDIT = 8_388_608
#: Without a DATA or PROGRESS frame for this long, the stream is dead.
IDLE_SECONDS = 30

_HEADER_BYTES = 13  # 4 length + 1 type + 8 sequence
_MAX_SEQUENCE = 2**64 - 1


class FrameTooLarge(Exception):
    """A frame beyond the cap — a connection fault, not a stream refusal."""


class FrameType(StrEnum):
    START = "start"
    DATA = "data"
    PROGRESS = "progress"
    END = "end"
    CANCEL = "cancel"
    ERROR = "error"


_TYPE_BYTES = {kind: index for index, kind in enumerate(FrameType)}
_BYTE_TYPES = dict(enumerate(FrameType))


@dataclass(frozen=True)
class Frame:
    type: FrameType
    sequence: int
    payload: bytes


class StreamRefusal(StrEnum):
    SEQUENCE_GAP = "sequence_gap"
    OVER_DECLARED_TOTAL = "over_declared_total"
    DIGEST_MISMATCH = "digest_mismatch"
    DATA_AFTER_END = "data_after_end"
    CREDIT_EXCEEDED = "credit_exceeded"
    TOTAL_ABOVE_LIMIT = "total_above_limit"
    TOTAL_MISMATCH = "total_mismatch"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class ReceiverDecision:
    ok: bool
    reason: StreamRefusal | None = None
    #: True when the stream reached a terminal state by agreement — a verified
    #: END or an explicit CANCEL/ERROR — rather than by refusal.
    finished: bool = False


_ACCEPTED = ReceiverDecision(ok=True)
_FINISHED = ReceiverDecision(ok=True, finished=True)


def encode_frame(frame: Frame) -> bytes:
    if len(frame.payload) > MAX_FRAME_PAYLOAD:
        raise FrameTooLarge(f"{len(frame.payload)} bytes in one frame")
    if not 0 <= frame.sequence <= _MAX_SEQUENCE:
        raise ValueError(f"sequence out of range: {frame.sequence}")
    return (
        len(frame.payload).to_bytes(4, "big")
        + bytes([_TYPE_BYTES[frame.type]])
        + frame.sequence.to_bytes(8, "big")
        + frame.payload
    )


def decode_frame(buffer: bytes) -> tuple[Frame, bytes] | None:
    """One frame off the front of the buffer, or None to read more.

    Returning None never consumes anything: the caller keeps its buffer and
    appends. An oversize length or unknown type raises, because after either
    the byte stream can no longer be trusted to frame anything.
    """
    if len(buffer) < _HEADER_BYTES:
        return None
    length = int.from_bytes(buffer[:4], "big")
    if length > MAX_FRAME_PAYLOAD:
        raise FrameTooLarge(f"declared payload of {length} bytes")
    kind = _BYTE_TYPES.get(buffer[4])
    if kind is None:
        raise ValueError(f"unknown frame type byte: {buffer[4]}")
    end = _HEADER_BYTES + length
    if len(buffer) < end:
        return None
    sequence = int.from_bytes(buffer[5:_HEADER_BYTES], "big")
    return Frame(kind, sequence, buffer[_HEADER_BYTES:end]), buffer[end:]


class StreamReceiver:
    """One inbound stream's rules, decided frame by frame.

    Any refusal is terminal: a peer that broke the protocol once does not get
    to keep talking on the same stream.
    """

    def __init__(self, *, declared_limit: int) -> None:
        if declared_limit <= 0:
            raise ValueError("a stream needs a positive server-side limit")
        self._limit = declared_limit
        self._declared_total: int | None = None
        self._next_sequence = 0
        self._outstanding = 0
        self._data_frames = 0
        self._hasher = hashlib.sha256()
        self._last_activity = 0.0
        self._terminal: ReceiverDecision | None = None
        self.received_bytes = 0

    def next_deadline(self) -> float:
        """When the transport must have seen a DATA or PROGRESS frame."""
        return self._last_activity + IDLE_SECONDS

    def credit(self, consumed_bytes: int) -> None:
        """The consumer took bytes off the receiver's hands; reopen the window."""
        if consumed_bytes < 0:
            raise ValueError("credit cannot be negative")
        self._outstanding = max(0, self._outstanding - consumed_bytes)

    def accept(self, frame: Frame, *, at: float = 0.0) -> ReceiverDecision:
        if self._terminal is not None:
            return ReceiverDecision(ok=False, reason=StreamRefusal.DATA_AFTER_END)
        decision = self._decide(frame)
        if decision.ok:
            self._last_activity = at
            self._next_sequence = frame.sequence + 1
            if decision.finished:
                self._terminal = decision
        elif decision.reason is not StreamRefusal.CREDIT_EXCEEDED:
            # Every refusal but backpressure is terminal. A credit overrun is
            # the transport being told to stop reading, not the peer lying;
            # the same frame is re-presented once the consumer catches up.
            self._terminal = decision
        return decision

    def _decide(self, frame: Frame) -> ReceiverDecision:
        if self._declared_total is None:
            return self._open(frame)
        if frame.sequence != self._next_sequence:
            return self._refuse(StreamRefusal.SEQUENCE_GAP)
        if frame.type is FrameType.DATA:
            return self._data(frame)
        if frame.type is FrameType.PROGRESS:
            return _ACCEPTED
        if frame.type is FrameType.END:
            return self._close(frame)
        if frame.type in (FrameType.CANCEL, FrameType.ERROR):
            return _FINISHED
        return self._refuse(StreamRefusal.MALFORMED)

    def _open(self, frame: Frame) -> ReceiverDecision:
        if frame.type is not FrameType.START or frame.sequence != 0:
            return self._refuse(StreamRefusal.MALFORMED)
        document = _json_object(frame.payload)
        total = None if document is None else document.get("total_limit")
        if not isinstance(total, int) or total < 0:
            return self._refuse(StreamRefusal.MALFORMED)
        if total > self._limit:
            return self._refuse(StreamRefusal.TOTAL_ABOVE_LIMIT)
        self._declared_total = total
        return _ACCEPTED

    def _data(self, frame: Frame) -> ReceiverDecision:
        if self._declared_total is None:  # pragma: no cover - _decide ordering
            return self._refuse(StreamRefusal.MALFORMED)
        if self.received_bytes + len(frame.payload) > self._declared_total:
            return self._refuse(StreamRefusal.OVER_DECLARED_TOTAL)
        if self._outstanding + len(frame.payload) > RECEIVE_CREDIT:
            return self._refuse(StreamRefusal.CREDIT_EXCEEDED)
        self.received_bytes += len(frame.payload)
        self._outstanding += len(frame.payload)
        self._data_frames += 1
        self._hasher.update(frame.payload)
        return _ACCEPTED

    def _close(self, frame: Frame) -> ReceiverDecision:
        document = _json_object(frame.payload)
        if document is None:
            return self._refuse(StreamRefusal.MALFORMED)
        total, count, digest = (
            document.get("total_bytes"),
            document.get("frame_count"),
            document.get("sha256"),
        )
        if not isinstance(total, int) or not isinstance(count, int) or not isinstance(digest, str):
            return self._refuse(StreamRefusal.MALFORMED)
        if total != self.received_bytes or count != self._data_frames:
            return self._refuse(StreamRefusal.TOTAL_MISMATCH)
        if digest != self._hasher.hexdigest():
            return self._refuse(StreamRefusal.DIGEST_MISMATCH)
        return _FINISHED

    def _refuse(self, reason: StreamRefusal) -> ReceiverDecision:
        return ReceiverDecision(ok=False, reason=reason)


def _json_object(payload: bytes) -> dict[str, object] | None:
    try:
        parsed: Any = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return cast(dict[str, object], parsed)
