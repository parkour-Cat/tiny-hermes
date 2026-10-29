import { describe, expect, test } from "vitest";

import { ApiError } from "./client";
import { hasWording, problemMessage } from "./messages";
import { enUS } from "../i18n/en-US";
import type { MessageKey } from "../i18n/zh-CN";
import { t as zh } from "../i18n/zh-CN";

const en = (key: MessageKey): string => enUS[key];

/**
 * This module used to import `t` from `../i18n/zh-CN` outright, so every
 * mapped error code rendered in Chinese however the user had set the locale
 * — silent, and worst exactly where a confused user is reading hardest. The
 * signature now demands a `t`, which makes the regression a compile error
 * rather than a wrong string; these pin the behaviour on top of that.
 */
describe("problemMessage renders in the caller's locale", () => {
  test("a mapped code follows the locale it is given", () => {
    const error = new ApiError(409, "approval_already_decided", "raw");
    expect(problemMessage(error, en)).toBe(enUS.approvalAlreadyDecided);
    expect(problemMessage(error, zh)).toBe(zh("approvalAlreadyDecided"));
    expect(problemMessage(error, en)).not.toBe(problemMessage(error, zh));
  });

  test("an unmapped code falls back to the server's own message, untranslated", () => {
    const error = new ApiError(500, "something_new", "the server said this");
    expect(problemMessage(error, en)).toBe("the server said this");
  });

  test("a non-ApiError with no message uses the caller's locale", () => {
    expect(problemMessage({}, en)).toBe(enUS.requestFailed);
  });
});

/* Every code an end user's routes can answer with, read from the backend's own
   source: one added there without wording here fails, instead of reaching the
   chat as an English sentence. */
const SOURCES = import.meta.glob<string>(
  [
    "../../../../packages/backend/src/tiny_hermes/*/presentation/end_user_*.py",
    "../../../../packages/backend/src/tiny_hermes/runs/presentation/errors.py",
    "../../../../packages/backend/src/tiny_hermes/session_workspace/presentation/files.py",
  ],
  { query: "?raw", import: "default", eager: true },
);
const CODES = [
  ...new Set(Object.values(SOURCES).flatMap((source) => [...source.matchAll(/code="([a-z_]+)"/g)].map((match) => match[1]!))),
];

/** In those files, but answered to the console's administrators, not to an end user. */
const NOT_THE_CHATS = ["channel_issuer_already_registered", "channel_issuer_not_found", "end_user_not_found", "invalid_channel_issuer"];

test("the end user's codes were found", () => {
  expect(CODES).toContain("end_user_credential_invalid");
  expect(CODES).toContain("workspace_files_changed");
  for (const code of NOT_THE_CHATS) expect(CODES).toContain(code);
});

test.each(CODES.filter((code) => !NOT_THE_CHATS.includes(code)))("%s has the chat's own wording", (code) => {
  expect(hasWording(code)).toBe(true);
});

test("a refusal whose reason the server composes keeps that reason under a translated heading", () => {
  const error = new ApiError(409, "run_not_steerable", "Nothing was added: the run has ended. Send it as a new message instead.");
  expect(problemMessage(error, zh)).toBe(
    "没有追加到当前任务，请作为新消息发送：Nothing was added: the run has ended. Send it as a new message instead.",
  );
});

test("a request the platform could not read is not offered as something to retry", () => {
  const message = problemMessage(new ApiError(422, "request_failed", ""), zh);
  expect(message).toBe(zh("requestInvalid"));
  expect(message).not.toContain("重试");
});

test("a refusal with no sentence at all says the request failed, in the caller's locale", () => {
  expect(problemMessage(new ApiError(500, "request_failed", ""), en)).toBe(enUS.requestFailed);
});
