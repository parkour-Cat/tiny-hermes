import { expect, test } from "vitest";
import { specDiff } from "./specDiff";
import type { AgentSpecDocument } from "../api/types";

const base: AgentSpecDocument = {
  schema_version: 1, personality: "help", model_policy: { provider: "openai_compatible", endpoint_id: "model1" }, tools: [],
  limits: { max_execution_seconds: 600, max_elapsed_seconds: 3600, max_model_calls: 12, max_tool_calls: 7, max_derived_retries: 2 },
  http_tools: [{ http_tool_version_id: "v1", operations: ["send"], write_policy: "governance" }],
};
test("diff exposes version and approval policy changes even when operation names stay the same", () => {
  const next = { ...base, http_tools: [{ ...base.http_tools![0]!, http_tool_version_id: "v2", write_policy: "disabled" as const }] };
  const keys = specDiff(base, next).map((row) => row.path);
  expect(keys).toContain("http_tools.0.http_tool_version_id");
  expect(keys).toContain("http_tools.0.write_policy");
});
test("model parameters and end user access are separate readable changes", () => {
  const rows = specDiff(base, { ...base, model_policy: { provider: "openai_compatible", endpoint_id: "model1", temperature: 0.2 }, end_user_access: { enabled: true } });
  expect(rows.map((row) => row.path)).toEqual(["end_user_access.enabled", "model_policy.temperature"]);
});
