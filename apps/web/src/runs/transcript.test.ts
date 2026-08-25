import { describe, expect, it } from "vitest";

import type { CanonicalMessage } from "../api/types";
import { textOf, transcriptLineOf } from "./transcript";

const message = (parts: CanonicalMessage["parts"], role = "assistant"): CanonicalMessage =>
  ({ role, parts }) as CanonicalMessage;

describe("transcriptLineOf", () => {
  it("shows what was said when the turn has words", () => {
    const line = transcriptLineOf(message([{ type: "text", text: "在查目录了。" }]));

    expect(line).toBe("在查目录了。");
  });

  it("names the tool a turn asked for", () => {
    // A turn that only called tools rendered as an empty row. The assistant
    // says "I'll try a few other paths", then a blank line, then a
    // conclusion — the step between them was invisible.
    const line = transcriptLineOf(
      message([
        { type: "tool_call", call_id: "c1", name: "file.read", arguments: { path: "/a" } },
      ]),
    );

    expect(line).toContain("file.read");
  });

  it("shows a tool's output rather than nothing", () => {
    const line = transcriptLineOf(
      message([{ type: "tool_result", call_id: "c1", output: "3 entries" }], "tool"),
    );

    expect(line).toContain("3 entries");
  });

  it("marks a failed tool result as failed", () => {
    // Success and failure rendered identically before, which made a
    // transcript of a failing Run read as though every step worked.
    const failed = transcriptLineOf(
      message(
        [{ type: "tool_result", call_id: "c1", output: "no such file", failed: true }],
        "tool",
      ),
    );
    const fine = transcriptLineOf(
      message([{ type: "tool_result", call_id: "c1", output: "no such file" }], "tool"),
    );

    expect(failed).not.toBe(fine);
  });

  it("truncates a long output instead of pasting the whole thing", () => {
    // The tools section below already carries the full output. This line
    // exists to keep the causal chain readable, and a 40 KB shell dump in
    // the middle of it does the opposite.
    const line = transcriptLineOf(
      message([{ type: "tool_result", call_id: "c1", output: "x".repeat(5000) }], "tool"),
    );

    expect(line.length).toBeLessThan(400);
  });

  it("says something for a turn with no readable part at all", () => {
    // Better a marker than a blank row: a blank row reads as a bug in the
    // page, which is exactly how this one was reported.
    const line = transcriptLineOf(message([{ type: "something_new_entirely" }]));

    expect(line.trim()).not.toBe("");
  });

  it("prefers the words when a turn both speaks and calls a tool", () => {
    const line = transcriptLineOf(
      message([
        { type: "text", text: "先看看目录。" },
        { type: "tool_call", call_id: "c1", name: "file.read", arguments: {} },
      ]),
    );

    expect(line).toContain("先看看目录。");
    expect(line).toContain("file.read");
  });
});

describe("textOf", () => {
  it("still returns only the words", () => {
    // Unchanged on purpose. `chat-web` decides whether to render a turn at
    // all with `textOf(message) !== ""`, and §19.1 keeps tool calls and
    // internal state off that surface — widening this would put them there.
    const line = textOf(
      message([
        { type: "text", text: "在查目录了。" },
        { type: "tool_call", call_id: "c1", name: "file.read", arguments: {} },
      ]),
    );

    expect(line).toBe("在查目录了。");
  });

  it("is empty for a turn that only carries a tool result", () => {
    expect(textOf(message([{ type: "tool_result", call_id: "c1", output: "x" }], "tool"))).toBe("");
  });
});
