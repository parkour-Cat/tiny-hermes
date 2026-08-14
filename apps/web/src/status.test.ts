import { expect, test } from "vitest";

import { zhCN } from "./i18n/zh-CN";
import { statusLabel } from "./status";

test("known codes are labelled in the operator's language", () => {
  expect(statusLabel("completed", (key) => zhCN[key])).toBe("已完成");
  expect(statusLabel("terminal", (key) => zhCN[key])).toBe("已结束");
  expect(statusLabel("developer", (key) => zhCN[key])).toBe("开发者");
  expect(statusLabel("active", (key) => zhCN[key])).toBe("启用");
  expect(statusLabel("assistant", (key) => zhCN[key])).toBe("助手");
});

test("a code the table does not know is left alone", () => {
  expect(statusLabel("run_completed", (key) => zhCN[key])).toBe("run_completed");
  expect(statusLabel("continue_once", (key) => zhCN[key])).toBe("continue_once");
});
