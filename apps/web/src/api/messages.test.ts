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
