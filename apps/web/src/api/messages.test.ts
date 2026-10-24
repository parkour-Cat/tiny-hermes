import { expect, test } from "vitest";
import { ApiError } from "./client";
import { problemMessage } from "./messages";
import { t } from "../i18n/zh-CN";

test("a refused context budget explains how to recover in Chinese", () => {
  const message = problemMessage(new ApiError(422, "context_budget_unsatisfied",
    "The context budget asks for 9472 tokens and this endpoint leaves 7168."), t);
  expect(message).toContain("上下文");
  expect(message).toContain("模型接入");
  expect(message).not.toContain("The context budget");
});

test.each([
  ["sandbox_memory_ceiling_exceeded", 8192, 4096],
  ["execution_ceiling_exceeded", 2000, 1800],
  ["round_ceiling_exceeded", 21, 20],
  ["tool_call_ceiling_exceeded", 201, 200],
])("a %s refusal is worded in the reader's language with both numbers", (code, asked, allowed) => {
  // On 2026-10-24 these four appeared in English on the Chinese console: the
  // server's own sentence, shown as it came. The numbers now travel apart.
  const message = problemMessage(
    new ApiError(422, code, "The agent asks for more than allowed.", { asked, allowed }),
    t,
  );
  expect(message).toContain(String(asked));
  expect(message).toContain(String(allowed));
  expect(message).not.toContain("The agent asks");
});

test("a ceiling refusal without its numbers falls back to the server's sentence", () => {
  // An older API, or a proxy that dropped `context`: saying something true
  // in English beats a Chinese sentence with holes in it.
  const message = problemMessage(
    new ApiError(422, "sandbox_memory_ceiling_exceeded", "The agent asks for a 8192 MB sandbox."),
    t,
  );
  expect(message).toBe("The agent asks for a 8192 MB sandbox.");
});
