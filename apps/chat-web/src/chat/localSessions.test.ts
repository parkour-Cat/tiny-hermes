import { afterEach, expect, test } from "vitest";

import {
  forgetAllSessionIds,
  forgetSessionId,
  loadKnownSessionIds,
  rememberSessionId,
} from "./localSessions";

const PREFIX = "tiny-hermes-chat-end-user-sessions:";

afterEach(() => {
  for (let i = window.localStorage.length - 1; i >= 0; i -= 1) {
    const key = window.localStorage.key(i);
    if (key !== null && key.startsWith(PREFIX)) {
      window.localStorage.removeItem(key);
    }
  }
});

// Task-9 review finding H: `localSessions` is keyed by alias with no way to
// clear it — a device that has talked to several Agents has no single "sign
// out" action, only per-alias `forgetSessionId` calls a caller would have to
// already know every alias to make. `forgetAllSessionIds` is that door: it
// walks every key this module ever wrote, regardless of alias, and leaves
// nothing behind for the next visitor to this device to inherit.

test("forgetAllSessionIds clears every alias this device remembered", () => {
  rememberSessionId("support-bot", "session-1");
  rememberSessionId("support-bot", "session-2");
  rememberSessionId("sales-bot", "session-3");

  forgetAllSessionIds();

  expect(loadKnownSessionIds("support-bot")).toEqual([]);
  expect(loadKnownSessionIds("sales-bot")).toEqual([]);
});

test("forgetAllSessionIds does not touch unrelated storage", () => {
  window.localStorage.setItem("some-other-app-key", "keep-me");
  rememberSessionId("support-bot", "session-1");

  forgetAllSessionIds();

  expect(window.localStorage.getItem("some-other-app-key")).toBe("keep-me");
  window.localStorage.removeItem("some-other-app-key");
});

test("forgetAllSessionIds on an empty device does not raise", () => {
  expect(() => forgetAllSessionIds()).not.toThrow();
});

test("forgetSessionId and forgetAllSessionIds compose: one alias, then everything", () => {
  rememberSessionId("support-bot", "session-1");
  rememberSessionId("support-bot", "session-2");
  forgetSessionId("support-bot", "session-1");
  expect(loadKnownSessionIds("support-bot")).toEqual(["session-2"]);

  forgetAllSessionIds();

  expect(loadKnownSessionIds("support-bot")).toEqual([]);
});
