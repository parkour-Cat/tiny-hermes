import { expect, test } from "vitest";

import { agentRef, chatPath, matchSessionId, resolveAgentRef, resolveChatRoute } from "./paths";
import type { ListedAgent } from "./published";

const acme = { id: "11111111-2222-4333-8444-555555555555", name: "Acme", status: "active" };
const other = { id: "99999999-aaaa-4bbb-8ccc-dddddddddddd", name: "Other", status: "active" };
const darwin = {
  id: "22222222-3333-4444-8555-666666666666",
  name: "Darwin",
  alias: "darwin",
  status: "published",
  current_version_id: "v1",
  created_at: "2026-08-10T00:00:00Z",
};

function row(workspace: typeof acme, alias = "darwin"): ListedAgent {
  return { workspace, agent: { ...darwin, alias, id: `${workspace.id.slice(0, 8)}-${alias}` } };
}

test("a unique alias is the whole path", () => {
  const agents = [row(acme)];
  expect(agentRef(agents[0]!, agents)).toBe("darwin");
  expect(chatPath(agents[0]!, agents, "33333333-4444-4555-8666-777777777777")).toBe(
    "/darwin/33333333",
  );
});

test("a clashing alias keeps a short workspace mark", () => {
  const agents = [row(acme), row(other)];
  expect(agentRef(agents[0]!, agents)).toBe("darwin--11111111");
  expect(resolveAgentRef("darwin--11111111", agents)?.workspace.id).toBe(acme.id);
  expect(resolveAgentRef("darwin", agents)).toBeUndefined();
});

test("uuid routes still resolve while alias routes wait for the list", () => {
  expect(
    resolveChatRoute(
      { left: acme.id, middle: darwin.id, right: "33333333-4444-4555-8666-777777777777" },
      [],
    ),
  ).toEqual({
    kind: "ok",
    workspaceId: acme.id,
    agentId: darwin.id,
    sessionRef: "33333333-4444-4555-8666-777777777777",
  });
  expect(resolveChatRoute({ left: "darwin" }, [])).toEqual({ kind: "pending" });
  expect(matchSessionId(["33333333-4444-4555-8666-777777777777"], "33333333")).toBe(
    "33333333-4444-4555-8666-777777777777",
  );
});
