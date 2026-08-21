import { expect, test } from "vitest";

import { chatPath, isAgentAlias, matchSessionId } from "./paths";

test("an alias alone is the whole path", () => {
  expect(chatPath("darwin")).toBe("/darwin");
  expect(chatPath("darwin", "33333333-4444-4555-8666-777777777777")).toBe("/darwin/33333333");
  expect(chatPath("darwin", null)).toBe("/darwin");
});

test("alias grammar matches the platform's own", () => {
  expect(isAgentAlias("support-bot")).toBe(true);
  expect(isAgentAlias("a")).toBe(true);
  expect(isAgentAlias("-leading-hyphen")).toBe(false);
  expect(isAgentAlias("trailing-hyphen-")).toBe(false);
  expect(isAgentAlias("Has-Caps")).toBe(false);
  expect(isAgentAlias(undefined)).toBe(false);
});

test("a short ref resolves against the known ids, exact match first", () => {
  const ids = ["33333333-4444-4555-8666-777777777777", "99999999-4444-4555-8666-777777777777"];
  expect(matchSessionId(ids, "33333333")).toBe(ids[0]);
  expect(matchSessionId(ids, ids[0]!)).toBe(ids[0]);
  expect(matchSessionId(ids, "unknown")).toBeNull();
  expect(matchSessionId(ids, null)).toBeNull();
});
