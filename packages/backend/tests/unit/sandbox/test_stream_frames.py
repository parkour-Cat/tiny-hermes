"""The framed streaming subprotocol, exercised entirely without sockets.

Design §5.3: typed frames, a 1 MiB frame cap, an 8 MiB receive credit, a
server-checked declared total, a running SHA-256, and a 30-second idle rule.
Everything here is a pure codec and a receiver state machine — the rules must
be testable on Windows, where the real socket transport is not.
"""

import hashlib
import json

import pytest
from tiny_hermes.sandbox.transport.frames import (
    IDLE_SECONDS,
    MAX_FRAME_PAYLOAD,
    RECEIVE_CREDIT,
    Frame,
    FrameTooLarge,
    FrameType,
    StreamReceiver,
    StreamRefusal,
    decode_frame,
    encode_frame,
)


def _start(total: int, *, sequence: int = 0) -> Frame:
    return Frame(
        FrameType.START, sequence, json.dumps({"total_limit": total}).encode()
    )


def _end(
    body: bytes, *, sequence: int, frame_count: int, sha256: str | None = None
) -> Frame:
    payload = {
        "total_bytes": len(body),
        "frame_count": frame_count,
        "sha256": sha256 or hashlib.sha256(body).hexdigest(),
    }
    return Frame(FrameType.END, sequence, json.dumps(payload).encode())


def test_frames_round_trip_and_reject_oversize() -> None:
    frame = Frame(FrameType.DATA, sequence=1, payload=b"x" * 100)
    encoded = encode_frame(frame)
    decoded = decode_frame(encoded + b"tail")
    assert decoded is not None
    parsed, rest = decoded
    assert parsed == frame
    assert rest == b"tail"
    with pytest.raises(FrameTooLarge):
        encode_frame(Frame(FrameType.DATA, 1, b"x" * (MAX_FRAME_PAYLOAD + 1)))


def test_half_a_frame_decodes_to_nothing_and_keeps_the_buffer() -> None:
    encoded = encode_frame(Frame(FrameType.DATA, 7, b"payload"))
    assert decode_frame(encoded[:4]) is None
    assert decode_frame(encoded[:-1]) is None
    assert decode_frame(b"") is None


def test_a_wire_length_beyond_the_cap_is_refused_before_buffering() -> None:
    poisoned = (MAX_FRAME_PAYLOAD + 1).to_bytes(4, "big") + bytes(9)
    with pytest.raises(FrameTooLarge):
        decode_frame(poisoned)


def test_receiver_refuses_a_sequence_gap_and_data_after_end() -> None:
    receiver = StreamReceiver(declared_limit=10_000)
    assert receiver.accept(_start(10_000)).ok
    assert receiver.accept(Frame(FrameType.DATA, 1, b"aa")).ok
    gap = receiver.accept(Frame(FrameType.DATA, 3, b"bb"))
    assert not gap.ok
    assert gap.reason is StreamRefusal.SEQUENCE_GAP


def test_data_after_the_end_frame_is_a_protocol_violation() -> None:
    body = b"whole"
    receiver = StreamReceiver(declared_limit=100)
    assert receiver.accept(_start(len(body))).ok
    assert receiver.accept(Frame(FrameType.DATA, 1, body)).ok
    ended = receiver.accept(_end(body, sequence=2, frame_count=1))
    assert ended.ok and ended.finished
    late = receiver.accept(Frame(FrameType.DATA, 3, b"more"))
    assert not late.ok
    assert late.reason is StreamRefusal.DATA_AFTER_END


def test_a_declared_total_above_the_operation_limit_dies_at_start() -> None:
    receiver = StreamReceiver(declared_limit=1_000)
    refused = receiver.accept(_start(1_001))
    assert not refused.ok
    assert refused.reason is StreamRefusal.TOTAL_ABOVE_LIMIT


def test_bytes_beyond_the_declared_total_are_refused_mid_stream() -> None:
    receiver = StreamReceiver(declared_limit=10_000)
    assert receiver.accept(_start(3)).ok
    assert receiver.accept(Frame(FrameType.DATA, 1, b"ab")).ok
    over = receiver.accept(Frame(FrameType.DATA, 2, b"cd"))
    assert not over.ok
    assert over.reason is StreamRefusal.OVER_DECLARED_TOTAL


def test_credit_is_a_window_the_consumer_must_keep_opening() -> None:
    receiver = StreamReceiver(declared_limit=RECEIVE_CREDIT * 4)
    assert receiver.accept(_start(RECEIVE_CREDIT * 4)).ok
    chunk = b"x" * MAX_FRAME_PAYLOAD
    sequence = 1
    for _ in range(RECEIVE_CREDIT // MAX_FRAME_PAYLOAD):
        assert receiver.accept(Frame(FrameType.DATA, sequence, chunk)).ok
        sequence += 1
    stalled = receiver.accept(Frame(FrameType.DATA, sequence, b"y"))
    assert not stalled.ok
    assert stalled.reason is StreamRefusal.CREDIT_EXCEEDED

    # The consumer catches up; the window reopens exactly that far.
    receiver.credit(MAX_FRAME_PAYLOAD)
    reopened = receiver.accept(Frame(FrameType.DATA, sequence, b"y"))
    assert reopened.ok


def test_end_frame_must_carry_matching_totals_and_digest() -> None:
    body = b"measured"
    receiver = StreamReceiver(declared_limit=100)
    assert receiver.accept(_start(len(body))).ok
    assert receiver.accept(Frame(FrameType.DATA, 1, body)).ok
    forged = receiver.accept(
        _end(body, sequence=2, frame_count=1, sha256="0" * 64)
    )
    assert not forged.ok
    assert forged.reason is StreamRefusal.DIGEST_MISMATCH


def test_end_frame_totals_are_checked_against_what_arrived() -> None:
    receiver = StreamReceiver(declared_limit=100)
    assert receiver.accept(_start(50)).ok
    assert receiver.accept(Frame(FrameType.DATA, 1, b"abc")).ok
    lying = json.dumps(
        {
            "total_bytes": 2,
            "frame_count": 1,
            "sha256": hashlib.sha256(b"abc").hexdigest(),
        }
    ).encode()
    refused = receiver.accept(Frame(FrameType.END, 2, lying))
    assert not refused.ok
    assert refused.reason is StreamRefusal.TOTAL_MISMATCH

    wrong_count = _end(b"abc", sequence=2, frame_count=9)
    # A refused END is terminal: the stream failed. Build a fresh receiver to
    # prove the frame-count check independently.
    fresh = StreamReceiver(declared_limit=100)
    assert fresh.accept(_start(50)).ok
    assert fresh.accept(Frame(FrameType.DATA, 1, b"abc")).ok
    counted = fresh.accept(wrong_count)
    assert not counted.ok
    assert counted.reason is StreamRefusal.TOTAL_MISMATCH


def test_cancel_mid_stream_ends_the_transfer_without_a_digest() -> None:
    receiver = StreamReceiver(declared_limit=100)
    assert receiver.accept(_start(50)).ok
    assert receiver.accept(Frame(FrameType.DATA, 1, b"abc")).ok
    cancelled = receiver.accept(Frame(FrameType.CANCEL, 2, b""))
    assert cancelled.ok and cancelled.finished
    late = receiver.accept(Frame(FrameType.DATA, 3, b"zzz"))
    assert not late.ok
    assert late.reason is StreamRefusal.DATA_AFTER_END


def test_a_stream_must_open_with_start_and_valid_json() -> None:
    receiver = StreamReceiver(declared_limit=100)
    early = receiver.accept(Frame(FrameType.DATA, 0, b"first"))
    assert not early.ok
    assert early.reason is StreamRefusal.MALFORMED

    fresh = StreamReceiver(declared_limit=100)
    garbled = fresh.accept(Frame(FrameType.START, 0, b"not json"))
    assert not garbled.ok
    assert garbled.reason is StreamRefusal.MALFORMED


def test_idle_and_operation_deadlines_are_decisions_not_sleeps() -> None:
    # The receiver is told the clock; it never reads one. `next_deadline()`
    # returns when the caller must have seen a DATA or PROGRESS frame.
    receiver = StreamReceiver(declared_limit=100)
    assert receiver.accept(_start(50), at=100.0).ok
    assert receiver.next_deadline() == 100.0 + IDLE_SECONDS
    assert receiver.accept(Frame(FrameType.DATA, 1, b"a"), at=110.0).ok
    assert receiver.next_deadline() == 110.0 + IDLE_SECONDS
    # PROGRESS is exactly the keep-alive that moves the deadline without bytes.
    assert receiver.accept(Frame(FrameType.PROGRESS, 2, b""), at=125.0).ok
    assert receiver.next_deadline() == 125.0 + IDLE_SECONDS


def test_progress_frames_do_not_count_toward_end_totals() -> None:
    """A silent exec's keep-alives must not look like output to the digest."""
    receiver = StreamReceiver(declared_limit=100)
    assert receiver.accept(_start(50)).ok
    assert receiver.accept(Frame(FrameType.PROGRESS, 1, b"")).ok
    assert receiver.accept(Frame(FrameType.DATA, 2, b"ab")).ok
    assert receiver.accept(Frame(FrameType.PROGRESS, 3, b"")).ok
    done = receiver.accept(_end(b"ab", sequence=4, frame_count=1))
    assert done.ok and done.finished
    assert receiver.received_bytes == 2


def test_the_running_digest_hashes_what_actually_arrived() -> None:
    parts = [b"alpha", b"beta", b"gamma"]
    body = b"".join(parts)
    receiver = StreamReceiver(declared_limit=100)
    assert receiver.accept(_start(len(body))).ok
    for offset, part in enumerate(parts):
        assert receiver.accept(Frame(FrameType.DATA, offset + 1, part)).ok
    done = receiver.accept(_end(body, sequence=len(parts) + 1, frame_count=len(parts)))
    assert done.ok and done.finished
    assert receiver.received_bytes == len(body)
