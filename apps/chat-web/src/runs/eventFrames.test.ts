import { expect, test } from "vitest";

import { readFrames } from "./eventFrames";

function frame(sequence: number, eventType: string): string {
  const data = JSON.stringify({
    sequence,
    event_type: eventType,
    occurred_at: "2026-08-10T02:00:00+00:00",
    payload: { note: eventType },
  });
  return `id: ${sequence}\nevent: ${eventType}\ndata: ${data}\n\n`;
}

test("a whole frame parses into sequence, type, time, and payload", () => {
  const { frames, rest } = readFrames(frame(4, "run_slice_ended"));

  expect(frames).toEqual([
    {
      sequence: 4,
      event_type: "run_slice_ended",
      occurred_at: "2026-08-10T02:00:00+00:00",
      payload: { note: "run_slice_ended" },
    },
  ]);
  expect(rest).toBe("");
});

test("a frame split across two chunks is emitted once, when its remainder arrives", () => {
  const whole = frame(1, "run_created");
  const half = whole.slice(0, 30);

  const first = readFrames(half);
  expect(first.frames).toEqual([]);
  expect(first.rest).toBe(half);

  const second = readFrames(first.rest + whole.slice(30));
  expect(second.frames.map((event) => event.sequence)).toEqual([1]);
  expect(second.rest).toBe("");
});

test("two frames in one chunk both parse, in the order they were sent", () => {
  const { frames, rest } = readFrames(frame(1, "run_created") + frame(2, "run_lease_acquired"));

  expect(frames.map((event) => event.event_type)).toEqual(["run_created", "run_lease_acquired"]);
  expect(rest).toBe("");
});

test("a trailing partial frame stays in the rest and is never emitted", () => {
  const whole = frame(7, "run_completed");
  const { frames, rest } = readFrames(`${whole}id: 8\nevent: run_fail`);

  expect(frames.map((event) => event.sequence)).toEqual([7]);
  expect(rest).toBe("id: 8\nevent: run_fail");
});

test("a heartbeat comment is skipped, and the frames around it still parse", () => {
  const { frames, rest } = readFrames(`${frame(1, "run_created")}: heartbeat\n\n${frame(2, "run_lease_acquired")}`);

  expect(frames.map((event) => event.sequence)).toEqual([1, 2]);
  expect(rest).toBe("");
});
