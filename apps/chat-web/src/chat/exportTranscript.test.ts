import { expect, test } from "vitest";

import { exportFilename, transcriptMarkdown } from "./exportTranscript";

test("the filename uses the alias and a short session id", () => {
  expect(exportFilename("darwin", "33333333-4444-4555-8666-777777777777")).toBe(
    "darwin-33333333.md",
  );
  expect(exportFilename("Weird Name!", null)).toBe("Weird-Name-chat.md");
});

test("a transcript becomes markdown the person can keep", () => {
  const markdown = transcriptMarkdown(
    "Darwin",
    [
      { role: "user", parts: [{ type: "text", text: "Summarize yesterday" }] },
      { role: "assistant", parts: [{ type: "text", text: "Here is the summary." }] },
      {
        role: "assistant",
        parts: [{ type: "tool_call", call_id: "c1", name: "file.read", arguments: { path: "a.md" } }],
      },
    ],
    { user: "用户", agent: "智能体" },
  );
  expect(markdown).toContain("# Darwin");
  expect(markdown).toContain("## 用户");
  expect(markdown).toContain("Summarize yesterday");
  expect(markdown).toContain("## 智能体");
  expect(markdown).toContain("Here is the summary.");
  expect(markdown).toContain("### file.read");
  expect(markdown).toContain('"path": "a.md"');
});
