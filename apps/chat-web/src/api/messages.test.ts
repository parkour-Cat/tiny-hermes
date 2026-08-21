import { describe, expect, test } from "vitest";

import { ApiError } from "./client";
import { problemMessage } from "./messages";
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
