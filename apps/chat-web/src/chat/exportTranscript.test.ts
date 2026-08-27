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

test("an exported transcript says which turns were taken back", () => {
  // 导出的文件是用户留在手上的那一份。如果它把撤回和没撤回的渲染成一样，
  // 那份文件就成了一个比界面更持久的误会。
  const markdown = transcriptMarkdown(
    "Darwin",
    [
      {
        role: "user",
        parts: [{ type: "text", text: "Summarize yesterday" }],
        withdrawn_at: "2026-08-26T10:43:00Z",
      },
      { role: "assistant", parts: [{ type: "text", text: "Here is the summary." }] },
    ],
    { user: "用户", agent: "智能体" },
  );
  expect(markdown).toContain("Summarize yesterday");
  expect(markdown).toMatch(/##\s*用户.*已撤回/);
  expect(markdown).not.toMatch(/##\s*智能体.*已撤回/);
});
