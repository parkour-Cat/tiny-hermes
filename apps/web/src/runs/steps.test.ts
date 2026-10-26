import { expect, test } from "vitest";

import { stepOf } from "./steps";
import type { ToolRound } from "./transcript";
import { t } from "../i18n/zh-CN";

function round(name: string, args: Record<string, unknown>): ToolRound {
  return { callId: "c", name, arguments: args, output: "", status: "succeeded", artifactIds: [] };
}

test("a sandbox command reads as running something, with the first line of the command", () => {
  expect(stepOf(round("shell.exec", { command: "cd out && python3 make_ppt.py\necho done" }), t)).toEqual({
    title: "在沙箱里运行",
    detail: "cd out && python3 make_ppt.py",
  });
});

test("an HTTP tool says which tool and operation, and what it was asked", () => {
  // The search call is the one an operator checks most: what was searched.
  expect(stepOf(round("http.web-search.search", { body: { query: "Gemini Argon 模型", max_results: 10 } }), t)).toEqual({
    title: "调用 web-search · search",
    detail: "Gemini Argon 模型",
  });
});

test("an HTTP tool without a query shows its arguments compactly", () => {
  expect(stepOf(round("http.orders-api.list_orders", { query: { region: "华东" } }), t).detail).toBe('{"query":{"region":"华东"}}');
});

test("platform tools read as what they do to the person's files", () => {
  expect(stepOf(round("file.send", { path: "out/a.pptx" }), t)).toEqual({ title: "把文件发回对话", detail: "out/a.pptx" });
  expect(stepOf(round("file.read", { path: "inbox/a.xlsx" }), t)).toEqual({ title: "读文件", detail: "inbox/a.xlsx" });
  expect(stepOf(round("session.search", { query: "上周的报表" }), t)).toEqual({ title: "搜索以前的会话", detail: "上周的报表" });
});

test("a tool this page does not know keeps its own name", () => {
  expect(stepOf(round("terminal", {}), t)).toEqual({ title: "terminal", detail: "" });
});

test("a long detail is cut rather than wrapping a whole script into the list", () => {
  expect(stepOf(round("shell.exec", { command: "x".repeat(300) }), t).detail).toHaveLength(121);
});
