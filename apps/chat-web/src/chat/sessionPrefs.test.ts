import { afterEach, expect, test } from "vitest";

import {
  arrangeSessions,
  emptySessionPrefs,
  hideSession,
  loadSessionPrefs,
  saveSessionPrefs,
  setArchived,
  setPinned,
} from "./sessionPrefs";

afterEach(() => {
  window.localStorage.removeItem("tiny-hermes-chat-session-prefs");
});

test("a missing store is an empty preference", () => {
  expect(loadSessionPrefs()).toEqual(emptySessionPrefs());
});

test("pin, archive, and hide only change this device's list", () => {
  const rows = [{ id: "a" }, { id: "b" }, { id: "c" }];
  let prefs = emptySessionPrefs();
  prefs = setPinned(prefs, "c", true);
  expect(arrangeSessions(rows, prefs).open.map((row) => row.id)).toEqual(["c", "a", "b"]);
  prefs = setArchived(prefs, "a", true);
  expect(arrangeSessions(rows, prefs)).toEqual({
    open: [{ id: "c" }, { id: "b" }],
    archived: [{ id: "a" }],
  });
  prefs = hideSession(prefs, "c");
  expect(arrangeSessions(rows, prefs)).toEqual({
    open: [{ id: "b" }],
    archived: [{ id: "a" }],
  });
  saveSessionPrefs(prefs);
  expect(loadSessionPrefs()).toEqual(prefs);
});
