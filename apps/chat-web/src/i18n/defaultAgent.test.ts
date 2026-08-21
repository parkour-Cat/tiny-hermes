import { afterEach, expect, test } from "vitest";

import {
  chooseDefaultAgent,
  loadDefaultAgent,
  saveDefaultAgent,
  sameDefaultAgent,
} from "./defaultAgent";

afterEach(() => {
  window.localStorage.removeItem("tiny-hermes-chat-default-agent");
});

test("a missing or broken store is treated as no preference", () => {
  expect(loadDefaultAgent()).toBeNull();
  window.localStorage.setItem("tiny-hermes-chat-default-agent", "{");
  expect(loadDefaultAgent()).toBeNull();
});

test("the stored agent is the one home opens first", () => {
  const newton = { workspaceId: "ws-1", agentId: "ag-newton" };
  saveDefaultAgent(newton);
  expect(loadDefaultAgent()).toEqual(newton);
  expect(sameDefaultAgent(newton, newton)).toBe(true);

  const rows = [
    { workspace: { id: "ws-1" }, agent: { id: "ag-darwin" } },
    { workspace: { id: "ws-1" }, agent: { id: "ag-newton" } },
  ];
  expect(chooseDefaultAgent(rows, newton)?.agent.id).toBe("ag-newton");
  expect(chooseDefaultAgent(rows, { workspaceId: "ws-1", agentId: "gone" })?.agent.id).toBe(
    "ag-darwin",
  );
  expect(chooseDefaultAgent(rows, null)?.agent.id).toBe("ag-darwin");
});
