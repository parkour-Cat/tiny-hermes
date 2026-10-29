import { expect, test } from "vitest";
import { ApiError } from "./client";
import { hasWording, problemMessage } from "./messages";
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

/* Every code the platform can answer with, read from the backend's own source
   rather than listed here: a code added there without wording here fails this
   test, instead of reaching a Chinese page as an English sentence. */
const SOURCES = import.meta.glob<string>("../../../../packages/backend/src/**/*.py", {
  query: "?raw",
  import: "default",
  eager: true,
});
const CODES = [
  ...new Set(Object.values(SOURCES).flatMap((source) => [...source.matchAll(/code="([a-z_]+)"/g)].map((match) => match[1]!))),
];

/** Codes the console never shows as a refusal, and why. */
const NOT_THE_CONSOLES: Record<string, string> = {
  channel_delivery_refused: "answered to a channel's webhook, not to a page",
  channel_key_required: "answered to a channel's webhook, not to a page",
  channel_key_unknown: "answered to a channel's webhook, not to a page",
  end_user_access_gate_closed: "the chat's: an end user's access",
  end_user_agent_not_found: "the chat's: an end user's access",
  end_user_credential_invalid: "the chat's: an end user's credential",
  end_user_credential_lifetime_exceeds_ceiling: "the chat's: an end user's credential",
  end_user_origin_not_allowed: "the chat's: the page an end user opened it from",
  credential_material: "a scan finding, listed by path, not a refusal",
  missing_reference: "a scan finding, listed by path, not a refusal",
  network_in_script: "a scan finding, listed by path, not a refusal",
  unreferenced_file: "a scan finding, listed by path, not a refusal",
};

test("the platform's codes were found", () => {
  expect(CODES.length).toBeGreaterThan(100);
  for (const code of Object.keys(NOT_THE_CONSOLES)) expect(CODES).toContain(code);
});

test.each(CODES.filter((code) => !(code in NOT_THE_CONSOLES)))("%s has the console's own wording", (code) => {
  expect(hasWording(code)).toBe(true);
});

test("a refusal the server words the same every time reads wholly in the reader's language", () => {
  const message = problemMessage(
    new ApiError(422, "secret_name_taken", "That name already belongs to a secret in this scope."),
    t,
  );
  expect(message).toContain("密钥");
  expect(message).not.toMatch(/[A-Za-z]{4,}/);
});

test("a refusal whose reason the server composes keeps that reason under a translated heading", () => {
  // Dozens of reasons stand behind codes like this one. Guessing which was
  // meant from the code alone would drop the one thing the reader needs.
  const message = problemMessage(
    new ApiError(422, "skill_import_failed", "A skill can only be imported over HTTPS."),
    t,
  );
  expect(message).toBe("技能导入失败：A skill can only be imported over HTTPS.");
});

test("a composed refusal without its reason is the heading alone", () => {
  expect(problemMessage(new ApiError(422, "skill_import_failed", ""), t)).toBe("技能导入失败");
});

test("a request the platform could not read is not offered as something to retry", () => {
  // FastAPI's own 422 carries no code: the input is wrong, and retrying it
  // as it is can only be refused again.
  const message = problemMessage(new ApiError(422, "request_failed", ""), t);
  expect(message).toBe(t("requestInvalid"));
  expect(message).not.toContain("重试");
});
