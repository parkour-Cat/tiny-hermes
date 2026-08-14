import { expect, test } from "vitest";

import { isBlankSession, sessionTitle } from "./sessionTitle";

test("uses the first user line, not a session id", () => {
  expect(
    sessionTitle(
      [
        { role: "assistant", parts: [{ type: "text", text: "Ready." }] },
        { role: "user", parts: [{ type: "text", text: "Summarize the report\nwith sources" }] },
      ],
      "未命名对话",
    ),
  ).toBe("Summarize the report");
});

test("an empty thread keeps the empty label", () => {
  expect(sessionTitle([], "未命名对话")).toBe("未命名对话");
});

test("a session is blank until the user has spoken", () => {
  const unused = { head_run_id: null, next_run_sequence: 1 };
  expect(isBlankSession(unused)).toBe(true);
  expect(isBlankSession({ head_run_id: "run-1", next_run_sequence: 2 })).toBe(false);
  expect(isBlankSession({ head_run_id: "run-1", next_run_sequence: 2 }, [])).toBe(true);
  expect(
    isBlankSession(unused, [{ role: "user", parts: [{ type: "text", text: "Hello" }] }]),
  ).toBe(false);
});

test("long first lines are clipped for the rail", () => {
  const line = "Please write a very long brief that should not stretch the session rail forever";
  expect(sessionTitle([{ role: "user", parts: [{ type: "text", text: line }] }], "新对话")).toBe(
    `${line.slice(0, 35)}…`,
  );
});
