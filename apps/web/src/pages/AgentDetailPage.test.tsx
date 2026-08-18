import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { AgentDetailPage } from "./AgentDetailPage";
import { TestTheme } from "../test/TestTheme";
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
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([])),
    // The skill picker reads the catalog. Empty by default: an Agent with no
    // skills is the ordinary case and every other test in this file is about
    // something else.
    http.get("/api/v1/skills", () => HttpResponse.json([])),
    // The network picker reads what the workspace approved. Empty by default,
    // which is what an Agent that asks for no network is measured against.
    http.get("/api/v1/outbound-scopes/workspace", () => HttpResponse.json([])),
    // The two tool catalogs, empty for the same reason the skills are.
    http.get("/api/v1/http-tools", () => HttpResponse.json([])),
    http.get("/api/v1/mcp-servers", () => HttpResponse.json([])),
  );
}

const HTTP_VERSION = "55555555-6666-4777-8888-999999999999";
const MCP_VERSION = "66666666-7777-4888-8999-aaaaaaaaaaaa";

/** One registered HTTP tool with a read and a write, and one MCP server. */
function loadedToolCatalogs(): void {
  server.use(
    http.get("/api/v1/http-tools", () =>
      HttpResponse.json([
        {
          id: "t1",
          workspace_id: WORKSPACE,
          name: "orders",
          base_url: "https://api.example.com/v2",
          credential_ref: null,
          current_version_id: HTTP_VERSION,
          created_at: "2026-08-18T00:00:00Z",
          updated_at: "2026-08-18T00:00:00Z",
        },
      ]),
    ),
    http.get("/api/v1/http-tools/t1/versions", () =>
      HttpResponse.json([
        {
          id: HTTP_VERSION,
          http_tool_id: "t1",
          version_number: 1,
          content_hash: "abc",
          title: "Orders",
          document_version: "1",
          operations: [
            {
              operation_id: "listOrders",
              method: "GET",
              path: "/orders",
              summary: null,
              read_only: true,
            },
            {
              operation_id: "createOrder",
              method: "POST",
              path: "/orders",
              summary: null,
              read_only: false,
            },
          ],
          status: "active",
          bindable: true,
          created_at: "2026-08-18T00:00:00Z",
        },
      ]),
    ),
    http.get("/api/v1/mcp-servers", () =>
      HttpResponse.json([
        {
          id: "s1",
          workspace_id: WORKSPACE,
          name: "docs",
          url: "https://mcp.example.com",
          credential_ref: null,
          current_version_id: MCP_VERSION,
          last_validated_at: "2026-08-18T00:00:00Z",
          created_at: "2026-08-18T00:00:00Z",
          updated_at: "2026-08-18T00:00:00Z",
        },
      ]),
    ),
    http.get("/api/v1/mcp-servers/s1/versions", () =>
      HttpResponse.json([
        {
          id: MCP_VERSION,
          mcp_server_id: "s1",
          version_number: 1,
          content_hash: "abc",
          tools: [
            { name: "search", description: null, input_schema: {} },
            { name: "purge", description: null, input_schema: {} },
          ],
          status: "active",
          bindable: true,
          created_at: "2026-08-18T00:00:00Z",
        },
      ]),
    ),
  );
}

const SKILL = "33333333-4444-4555-8666-777777777777";
const SKILL_VERSION = "44444444-5555-4666-8777-888888888888";

/** A catalog with one skill in it, and one bindable version of that skill. */
function loadedCatalog(): void {
  server.use(
    http.get("/api/v1/skills", () =>
      HttpResponse.json([
        {
          id: SKILL,
          scope: "workspace",
          workspace_id: WORKSPACE,
          name: "rollout",
          current_version_id: SKILL_VERSION,
          created_at: "2026-08-18T00:00:00Z",
          updated_at: "2026-08-18T00:00:00Z",
        },
      ]),
    ),
    http.get(`/api/v1/skills/${SKILL}/versions`, () =>
      HttpResponse.json([
        {
          id: SKILL_VERSION,
          skill_id: SKILL,
          version_number: 1,
          content_hash: "c".repeat(64),
          name: "rollout",
          description: "How to drain a machine.",
          findings: [],
          source: "upload",
          source_url: null,
          source_ref: null,
          status: "active",
          bindable: true,
          created_at: "2026-08-18T00:00:00Z",
        },
      ]),
    ),
  );
}

function renderDetail(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
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
    </TestTheme>,
  );
}

test("the loaded draft fills every field the console can edit", async () => {
  loadedAgent();

  renderDetail();

  expect(await screen.findByLabelText("人格")).toHaveValue("You answer support questions.");
  expect(screen.getByText("continue_once")).toBeInTheDocument();
  for (const tool of ["file.list", "file.read", "file.write", "shell.exec"]) {
    expect(screen.getByRole("checkbox", { name: tool })).not.toBeChecked();
  }
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

test("the tools checklist puts the bound names on the draft", async () => {
  loadedAgent(3);
  const sent: unknown[] = [];
  server.use(
    http.put(`/api/v1/agents/${AGENT}/draft`, async ({ request }) => {
      sent.push(await request.json());
      return HttpResponse.json(draftBody(4));
    }),
  );

  renderDetail();
  await userEvent.click(await screen.findByRole("checkbox", { name: "file.list" }));
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent).toEqual([
    {
      expected_revision: 3,
      spec: { ...SPEC, tools: ["file.list"] },
    },
  ]);
});

const HASH = "9f2c4b7a1d3e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff";

function version(number: number) {
  return {
    id: `v${number}`,
    agent_id: AGENT,
    version_number: number,
    schema_version: 1,
    content_hash: HASH,
    created_at: "2026-08-10T02:00:00Z",
  };
}

test("publishing asks first, and sends nothing while the question is open", async () => {
  loadedAgent(3);
  let attempts = 0;
  server.use(
    http.post(`/api/v1/agents/${AGENT}/publish`, () => {
      attempts += 1;
      return HttpResponse.json(version(1), { status: 201 });
    }),
  );

  renderDetail();
  await userEvent.click(await screen.findByRole("button", { name: "发布" }));

  // The half a confirmation actually protects: the request that was not sent.
  expect(await screen.findByText("将把草稿修订 3 发布为新版本。")).toBeInTheDocument();
  expect(attempts).toBe(0);
});

test("confirming publishes the loaded revision and shows the version it made", async () => {
  loadedAgent(3);
  const sent: unknown[] = [];
  const published = version(1);
  server.use(
    http.post(`/api/v1/agents/${AGENT}/publish`, async ({ request }) => {
      sent.push(await request.json());
      return HttpResponse.json(published, { status: 201 });
    }),
    http.get(`/api/v1/agents/${AGENT}/versions/${published.id}`, () =>
      HttpResponse.json({ ...published, spec: SPEC }),
    ),
  );

  renderDetail();
  await userEvent.click(await screen.findByRole("button", { name: "发布" }));
  await userEvent.click(await screen.findByRole("button", { name: "确定" }));

  expect(await screen.findByText(`当前版本 v1`)).toBeInTheDocument();
  expect(screen.getAllByText(HASH).length).toBeGreaterThan(0);
  expect(sent).toEqual([{ expected_revision: 3 }]);
});

test("re-publishing unchanged content is reported as no new version", async () => {
  loadedAgent(3);
  const published = version(1);
  server.use(
    // 200 rather than 201: the content was already published, which the server
    // treats as success and so must the console.
    http.post(`/api/v1/agents/${AGENT}/publish`, () =>
      HttpResponse.json(published, { status: 200 }),
    ),
    http.get(`/api/v1/agents/${AGENT}/versions/${published.id}`, () =>
      HttpResponse.json({ ...published, spec: SPEC }),
    ),
  );

  renderDetail();
  await userEvent.click(await screen.findByRole("button", { name: "发布" }));
  await userEvent.click(await screen.findByRole("button", { name: "确定" }));

  expect(await screen.findByText("草稿内容与当前版本相同，没有产生新版本。")).toBeInTheDocument();
});

test("publishing a stale revision is handed back to the user", async () => {
  loadedAgent(3);
  let attempts = 0;
  server.use(
    http.post(`/api/v1/agents/${AGENT}/publish`, () => {
      attempts += 1;
      return HttpResponse.json(
        { code: "draft_revision_conflict", detail: "The agent draft changed after it was read." },
        { status: 409 },
      );
    }),
  );

  renderDetail();
  await userEvent.click(await screen.findByRole("button", { name: "发布" }));
  await userEvent.click(await screen.findByRole("button", { name: "确定" }));

  expect(
    await screen.findByText("草稿已被改动，你的修改仍在表单中。请重新载入后再保存。"),
  ).toBeInTheDocument();
  await waitFor(() => expect(attempts).toBe(1));
});

test("escape closes the question and puts focus back on 发布", async () => {
  loadedAgent(3);

  renderDetail();
  const publish = await screen.findByRole("button", { name: "发布" });
  await userEvent.click(publish);
  await screen.findByRole("button", { name: "确定" });

  await userEvent.keyboard("{Escape}");

  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "确定" })).not.toBeInTheDocument(),
  );
  expect(publish).toHaveFocus();
});

test("the draft revision and the published version are two separate facts", async () => {
  loadedAgent(3);

  renderDetail();

  expect(await screen.findByText("草稿修订 3")).toBeInTheDocument();
  expect(screen.getByText("尚未发布")).toBeInTheDocument();
  expect(screen.getByText("尚未发布，没有可对比的版本。")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "打开调试台" })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/agents/${AGENT}/playground`,
  );
});

test("enabling delivery puts the timeout on the draft", async () => {
  loadedAgent(3);
  const sent: unknown[] = [];
  server.use(
    http.put(`/api/v1/agents/${AGENT}/draft`, async ({ request }) => {
      sent.push(await request.json());
      return HttpResponse.json(draftBody(4));
    }),
  );

  renderDetail();
  await userEvent.click(await screen.findByRole("switch", { name: "允许用 OpenAI 兼容接口调用" }));
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent).toEqual([
    {
      expected_revision: 3,
      spec: {
        ...SPEC,
        delivery: { enabled: true, sync_timeout_seconds: 60 },
      },
    },
  ]);
});

test("saving a name sends a patch, not a new draft revision", async () => {
  loadedAgent();
  const sent: unknown[] = [];
  server.use(
    http.patch(`/api/v1/agents/${AGENT}`, async ({ request }) => {
      sent.push(await request.json());
      return HttpResponse.json({ ...AGENT_ROW, name: "Renamed" });
    }),
  );

  renderDetail();
  const name = await screen.findByLabelText("名称");
  await userEvent.clear(name);
  await userEvent.type(name, "Renamed");
  await userEvent.click(screen.getByRole("button", { name: "保存" }));

  await waitFor(() => expect(sent).toEqual([{ name: "Renamed", alias: "analyst" }]));
  expect(await screen.findByRole("heading", { name: "Renamed" })).toBeInTheDocument();
});

test("a published version is compared field by field against the form", async () => {
  const published = version(1);
  server.use(
    http.get(`/api/v1/agents/${AGENT}`, () =>
      HttpResponse.json({
        ...AGENT_ROW,
        status: "published",
        current_version_id: published.id,
      }),
    ),
    http.get(`/api/v1/agents/${AGENT}/draft`, () => HttpResponse.json(draftBody(4))),
    http.get(`/api/v1/agents/${AGENT}/versions`, () => HttpResponse.json([published])),
    http.get(`/api/v1/agents/${AGENT}/versions/${published.id}`, () =>
      HttpResponse.json({ ...published, spec: SPEC }),
    ),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([])),
  );

  renderDetail();
  const personality = await screen.findByLabelText("人格");
  await userEvent.clear(personality);
  await userEvent.type(personality, "A different voice.");

  expect(await screen.findByText("personality")).toBeInTheDocument();
  expect(screen.getByText("You answer support questions.")).toBeInTheDocument();
  expect(screen.getAllByText("A different voice.").length).toBeGreaterThan(0);
});

test("rollback asks first, then names the version it restored", async () => {
  const first = version(1);
  const second = version(2);
  let body: unknown;
  server.use(
    http.get(`/api/v1/agents/${AGENT}`, () =>
      HttpResponse.json({
        ...AGENT_ROW,
        status: "published",
        current_version_id: second.id,
      }),
    ),
    http.get(`/api/v1/agents/${AGENT}/draft`, () => HttpResponse.json(draftBody(5))),
    http.get(`/api/v1/agents/${AGENT}/versions`, () => HttpResponse.json([first, second])),
    http.get(`/api/v1/agents/${AGENT}/versions/${second.id}`, () =>
      HttpResponse.json({ ...second, spec: SPEC }),
    ),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([])),
    http.post(`/api/v1/agents/${AGENT}/rollback`, async ({ request }) => {
      body = await request.json();
      return HttpResponse.json(first);
    }),
  );

  renderDetail();
  await userEvent.click(await screen.findByRole("button", { name: "回滚到此版本" }));
  expect(await screen.findByText("回滚会把该版本重新设为当前版本，草稿不会自动改写。")).toBeInTheDocument();
  expect(body).toBeUndefined();

  await userEvent.click(screen.getByRole("button", { name: "确定" }));
  await waitFor(() => expect(body).toEqual({ version_id: first.id }));
  expect(await screen.findByText("当前版本 v1")).toBeInTheDocument();
});


test("binding a skill puts its version id on the draft, never its name", async () => {
  loadedAgent(3);
  loadedCatalog();
  const sent: unknown[] = [];
  server.use(
    http.put(`/api/v1/agents/${AGENT}/draft`, async ({ request }) => {
      sent.push(await request.json());
      return HttpResponse.json(draftBody(4));
    }),
  );

  renderDetail();
  await userEvent.click(await screen.findByLabelText("技能"));
  await userEvent.click(await screen.findByTitle("rollout v1"));
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

  await waitFor(() => expect(sent).toHaveLength(1));
  // A version id, so publishing a new version of this skill changes nothing
  // about this Agent. Switching is another edit and another publish.
  expect(sent).toEqual([
    {
      expected_revision: 3,
      spec: { ...SPEC, skills: [{ skill_version_id: SKILL_VERSION }] },
    },
  ]);
});

test("a publish refused for summary budget names what each summary costs", async () => {
  loadedAgent(3);
  loadedCatalog();
  server.use(
    http.post(`/api/v1/agents/${AGENT}/publish`, () =>
      HttpResponse.json(
        {
          code: "skill_summary_budget_exceeded",
          title: "Skill summaries do not fit",
          detail: "The bound summaries estimate 1800 tokens and the segment holds 1536.",
          context: {
            summaries: [
              { skill: "rollout", estimated_tokens: 1200 },
              { skill: "postmortem", estimated_tokens: 600 },
            ],
          },
        },
        { status: 422 },
      ),
    ),
  );

  renderDetail();
  await userEvent.click(await screen.findByRole("button", { name: "发布" }));
  await userEvent.click(await screen.findByRole("button", { name: "确定" }));

  // Per summary rather than as one total: the author has to be able to see
  // which description is the expensive one.
  expect(await screen.findByText("rollout：约 1200 token")).toBeInTheDocument();
  expect(screen.getByText("postmortem：约 600 token")).toBeInTheDocument();
});


test("the network picker offers what the workspace approved and nothing else", async () => {
  loadedAgent(3);
  server.use(
    http.get("/api/v1/outbound-scopes/workspace", () =>
      HttpResponse.json([
        {
          id: "s1",
          level: "workspace",
          workspace_id: WORKSPACE,
          entry: "api.example.com",
          note: null,
          managed: false,
          created_at: "2026-08-18T00:00:00Z",
        },
      ]),
    ),
  );
  const sent: unknown[] = [];
  server.use(
    http.put(`/api/v1/agents/${AGENT}/draft`, async ({ request }) => {
      sent.push(await request.json());
      return HttpResponse.json(draftBody(4));
    }),
  );

  renderDetail();
  await userEvent.click(await screen.findByLabelText("网络"));
  await userEvent.click(await screen.findByTitle("api.example.com"));
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

  // Chosen, never typed: an entry outside the workspace's range is refused at
  // publish, and a free-text field would teach an author to publish and see.
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent).toEqual([
    {
      expected_revision: 3,
      spec: { ...SPEC, network: { allow: ["api.example.com"] } },
    },
  ]);
});

test("an agent that asks for no network publishes the document it always did", async () => {
  loadedAgent(3);
  const sent: unknown[] = [];
  server.use(
    http.put(`/api/v1/agents/${AGENT}/draft`, async ({ request }) => {
      sent.push(await request.json());
      return HttpResponse.json(draftBody(4));
    }),
  );

  renderDetail();
  await userEvent.click(await screen.findByRole("button", { name: "保存草稿" }));

  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent).toEqual([{ expected_revision: 3, spec: SPEC }]);
});

test("binding an HTTP operation stores the version id and the operation, and the write policy", async () => {
  loadedAgent(3);
  loadedToolCatalogs();
  const sent: unknown[] = [];
  server.use(
    http.put(`/api/v1/agents/${AGENT}/draft`, async ({ request }) => {
      sent.push(await request.json());
      return HttpResponse.json(draftBody(4));
    }),
  );

  renderDetail();
  await userEvent.click(await screen.findByLabelText("HTTP 操作"));
  await userEvent.click(await screen.findByTitle("POST createOrder · 会改数据"));
  // §16.3's choice, made in the builder rather than discovered at publish.
  await userEvent.click(screen.getByLabelText("HTTP 写操作怎么办"));
  await userEvent.click(await screen.findByTitle("每次都问管理员"));
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent).toEqual([
    {
      expected_revision: 3,
      spec: {
        ...SPEC,
        http_tools: [
          {
            http_tool_version_id: HTTP_VERSION,
            operations: ["createOrder"],
            write_policy: "governance",
          },
        ],
      },
    },
  ]);
});

test("an MCP binding names every tool, because there is no way to say all", async () => {
  loadedAgent(3);
  loadedToolCatalogs();
  const sent: unknown[] = [];
  server.use(
    http.put(`/api/v1/agents/${AGENT}/draft`, async ({ request }) => {
      sent.push(await request.json());
      return HttpResponse.json(draftBody(4));
    }),
  );

  renderDetail();
  await userEvent.click(await screen.findByLabelText("MCP 工具"));
  await userEvent.click(await screen.findByTitle("search"));
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

  await waitFor(() => expect(sent).toHaveLength(1));
  // `purge` was advertised and is not in the binding: a server's discovery is
  // not a permission.
  expect(sent).toEqual([
    {
      expected_revision: 3,
      spec: {
        ...SPEC,
        mcp_tools: [
          { mcp_server_version_id: MCP_VERSION, tools: ["search"], write_policy: null },
        ],
      },
    },
  ]);
});

test("an agent that binds no tool publishes the document it always did", async () => {
  loadedAgent(3);
  loadedToolCatalogs();
  const sent: unknown[] = [];
  server.use(
    http.put(`/api/v1/agents/${AGENT}/draft`, async ({ request }) => {
      sent.push(await request.json());
      return HttpResponse.json(draftBody(4));
    }),
  );

  renderDetail();
  await userEvent.click(await screen.findByRole("button", { name: "保存草稿" }));

  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent).toEqual([{ expected_revision: 3, spec: SPEC }]);
});
