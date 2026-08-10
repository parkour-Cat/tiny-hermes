import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { AgentDetailPage } from "./AgentDetailPage";
import { ConsoleTheme } from "../layout/ConsoleTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const AGENT = "22222222-3333-4444-8555-666666666666";

const SPEC = {
  schema_version: 1,
  personality: "You answer support questions.",
  model_policy: { provider: "deterministic", scenario: "continue_once" },
  tools: [],
  limits: {
    max_execution_seconds: 600,
    max_elapsed_seconds: 3600,
    max_model_calls: 12,
    max_tool_calls: 7,
    max_derived_retries: 2,
  },
};

const AGENT_ROW = {
  id: AGENT,
  name: "Analyst",
  alias: "analyst",
  status: "draft",
  current_version_id: null,
  created_at: "2026-08-10T00:00:00Z",
};

function draftBody(revision: number, personality = SPEC.personality) {
  return {
    agent_id: AGENT,
    revision,
    spec: { ...SPEC, personality },
    updated_at: "2026-08-10T01:00:00Z",
  };
}

/** The two reads the page always makes, plus an unpublished version history. */
function loadedAgent(revision = 3): void {
  server.use(
    http.get(`/api/v1/agents/${AGENT}`, () => HttpResponse.json(AGENT_ROW)),
    http.get(`/api/v1/agents/${AGENT}/draft`, () => HttpResponse.json(draftBody(revision))),
    http.get(`/api/v1/agents/${AGENT}/versions`, () => HttpResponse.json([])),
  );
}

function renderDetail(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <ConsoleTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/agents/${AGENT}`]}>
          <Routes>
            <Route
              path="/workspaces/:workspaceId/agents/:agentId"
              element={<AgentDetailPage />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </ConsoleTheme>,
  );
}

test("the loaded draft fills every field the console can edit", async () => {
  loadedAgent();

  renderDetail();

  expect(await screen.findByLabelText("人格")).toHaveValue("You answer support questions.");
  expect(screen.getByText("continue_once")).toBeInTheDocument();
  expect(screen.getByLabelText("单次执行秒数上限")).toHaveValue("600");
  expect(screen.getByLabelText("总时长秒数上限")).toHaveValue("3600");
  expect(screen.getByLabelText("模型调用次数上限")).toHaveValue("12");
  expect(screen.getByLabelText("工具调用次数上限")).toHaveValue("7");
  expect(screen.getByLabelText("派生重试次数上限")).toHaveValue("2");
});

test("the scenario list offers the three the platform actually implements", async () => {
  loadedAgent();

  renderDetail();
  await userEvent.click(await screen.findByLabelText("模型场景"));

  const offered = (await screen.findAllByRole("option")).map((option) => option.textContent);
  // A fourth option would be a claim about a substitute that only accepts
  // these three, and the request would come back 422.
  expect(offered).toEqual(["complete", "continue_once", "fail_replay_safe"]);
});

test("saving sends the loaded revision and a whole phase-two spec", async () => {
  loadedAgent(3);
  const sent: { body: unknown; csrf: string | null; workspace: string | null }[] = [];
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.put(`/api/v1/agents/${AGENT}/draft`, async ({ request }) => {
      sent.push({
        body: await request.json(),
        csrf: request.headers.get("X-CSRF-Token"),
        workspace: request.headers.get("X-Workspace-Id"),
      });
      return HttpResponse.json(draftBody(4, "Rewritten."));
    }),
  );

  renderDetail();
  const personality = await screen.findByLabelText("人格");
  await userEvent.clear(personality);
  await userEvent.type(personality, "Rewritten.");
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent).toEqual([
    {
      csrf: "token-value",
      workspace: WORKSPACE,
      body: { expected_revision: 3, spec: { ...SPEC, personality: "Rewritten." } },
    },
  ]);
  expect(await screen.findByText("草稿修订 4")).toBeInTheDocument();
});

test("a draft conflict keeps the typed personality and sends nothing more", async () => {
  loadedAgent(3);
  let attempts = 0;
  server.use(
    http.put(`/api/v1/agents/${AGENT}/draft`, () => {
      attempts += 1;
      return HttpResponse.json(
        { code: "draft_revision_conflict", detail: "The agent draft changed after it was read." },
        { status: 409 },
      );
    }),
  );

  renderDetail();
  const personality = await screen.findByLabelText("人格");
  await userEvent.clear(personality);
  await userEvent.type(personality, "Mine.");
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

  expect(
    await screen.findByText("草稿已被改动，你的修改仍在表单中。请重新载入后再保存。"),
  ).toBeInTheDocument();
  // Retrying with the server's new revision would overwrite whatever the other
  // writer saved and call it success.
  expect(personality).toHaveValue("Mine.");
  await waitFor(() => expect(attempts).toBe(1));
});

test("reloading the draft asks first, then replaces what was typed", async () => {
  let revision = 3;
  server.use(
    http.get(`/api/v1/agents/${AGENT}`, () => HttpResponse.json(AGENT_ROW)),
    http.get(`/api/v1/agents/${AGENT}/versions`, () => HttpResponse.json([])),
    http.get(`/api/v1/agents/${AGENT}/draft`, () =>
      HttpResponse.json(draftBody(revision, revision === 3 ? SPEC.personality : "Theirs.")),
    ),
  );

  renderDetail();
  const personality = await screen.findByLabelText("人格");
  await userEvent.clear(personality);
  await userEvent.type(personality, "Mine.");
  revision = 7;

  await userEvent.click(screen.getByRole("button", { name: "重新载入草稿" }));
  expect(personality).toHaveValue("Mine.");

  await userEvent.click(await screen.findByRole("button", { name: "确定" }));

  await waitFor(() => expect(screen.getByLabelText("人格")).toHaveValue("Theirs."));
  expect(await screen.findByText("草稿修订 7")).toBeInTheDocument();
});

test("a rejected spec is reported without throwing the edits away", async () => {
  loadedAgent(3);
  server.use(
    http.put(`/api/v1/agents/${AGENT}/draft`, () =>
      HttpResponse.json(
        {
          code: "invalid_agent_spec",
          detail: "The agent configuration is not a valid phase-two specification.",
        },
        { status: 422 },
      ),
    ),
  );

  renderDetail();
  const personality = await screen.findByLabelText("人格");
  await userEvent.clear(personality);
  await userEvent.type(personality, "Still mine.");
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

  expect(await screen.findByText("配置不符合平台的规范要求")).toBeInTheDocument();
  expect(personality).toHaveValue("Still mine.");
});

test("the tools gap is stated, not mocked up", async () => {
  loadedAgent();

  renderDetail();

  expect(await screen.findByText("工具在阶段三接入，当前草稿固定为空。")).toBeInTheDocument();
  // Nothing to press, nothing to fill: an inert control here would promise a
  // capability the platform does not have.
  expect(screen.queryByRole("button", { name: "添加工具" })).not.toBeInTheDocument();
});

test("the draft revision and the published version are two separate facts", async () => {
  loadedAgent(3);

  renderDetail();

  expect(await screen.findByText("草稿修订 3")).toBeInTheDocument();
  expect(screen.getByText("尚未发布")).toBeInTheDocument();
  expect(
    screen.getByText("接口不提供草稿内容摘要，控制台无法判断草稿与已发布版本是否一致。"),
  ).toBeInTheDocument();
});
