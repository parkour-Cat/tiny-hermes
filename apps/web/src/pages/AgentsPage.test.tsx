import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { AgentsPage } from "./AgentsPage";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";
import { t } from "../i18n/zh-CN";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const OTHER_WORKSPACE = "99999999-8888-4777-8666-555555555555";

type AgentRow = {
  id: string;
  name: string;
  alias: string;
  status: string;
  current_version_id: string | null;
  created_at: string;
};

function agent(overrides: Partial<AgentRow> = {}): AgentRow {
  return {
    id: "a1",
    name: "Analyst",
    alias: "analyst",
    status: "active",
    current_version_id: null,
    created_at: "2026-08-10T00:00:00Z",
    ...overrides,
  };
}

function renderAgents(workspace = WORKSPACE): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${workspace}/agents`]}>
          <Routes>
            <Route path="/workspaces/:workspaceId/agents" element={<AgentsPage />} />
            <Route
              path="/workspaces/:workspaceId/agents/:agentId"
              element={<p>builder for the new agent</p>}
            />
            <Route path="*" element={<p>somewhere else</p>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("the list is asked for inside the workspace the address names", async () => {
  const scopes: (string | null)[] = [];
  server.use(
    http.get("/api/v1/agents", ({ request }) => {
      scopes.push(request.headers.get("X-Workspace-Id"));
      return HttpResponse.json([agent()]);
    }),
  );

  renderAgents();

  expect(await screen.findByText("Analyst")).toBeInTheDocument();
  expect(scopes).toEqual([WORKSPACE]);
});

test("a published agent and an unpublished one are told apart", async () => {
  server.use(
    http.get("/api/v1/agents", () =>
      HttpResponse.json([
        agent({ id: "a1", name: "Analyst", alias: "analyst", current_version_id: null }),
        agent({ id: "a2", name: "Router", alias: "router", current_version_id: "v1" }),
      ]),
    ),
  );

  renderAgents();

  const analyst = await screen.findByRole("listitem", { name: "Analyst" });
  const router = screen.getByRole("listitem", { name: "Router" });
  expect(within(analyst).getByText("尚未发布")).toBeInTheDocument();
  expect(within(router).getByText("已发布")).toBeInTheDocument();
});

test("creating an agent sends its name and alias", async () => {
  const created: { name: string; alias: string }[] = [];
  const rows: AgentRow[] = [];
  server.use(
    http.get("/api/v1/agents", () => HttpResponse.json(rows)),
    http.post("/api/v1/agents", async ({ request }) => {
      const body = (await request.json()) as { name: string; alias: string };
      created.push(body);
      const row = agent({ id: "a9", name: body.name, alias: body.alias });
      rows.push(row);
      return HttpResponse.json(row, { status: 201 });
    }),
  );

  renderAgents();

  await userEvent.click(await screen.findByRole("button", { name: "新建 Agent" }));
  await userEvent.type(screen.getByLabelText("名称"), "Analyst");
  await userEvent.type(screen.getByLabelText("别名"), "analyst");
  await userEvent.click(screen.getByRole("button", { name: "创建" }));

  // A new Agent has no persona, model or tools, so creating one lands in the
  // builder rather than back on a list where the way in is the name in small
  // type.
  expect(await screen.findByText("builder for the new agent")).toBeInTheDocument();
  expect(created).toEqual([{ name: "Analyst", alias: "analyst" }]);
});

test("a taken alias keeps the dialog open and the typed values in it", async () => {
  let attempts = 0;
  server.use(
    http.get("/api/v1/agents", () => HttpResponse.json([])),
    http.post("/api/v1/agents", () => {
      attempts += 1;
      return HttpResponse.json(
        {
          code: "agent_alias_taken",
          detail: "Another agent in this workspace already uses that alias.",
        },
        { status: 409 },
      );
    }),
  );

  renderAgents();

  await userEvent.click(await screen.findByRole("button", { name: "新建 Agent" }));
  await userEvent.type(screen.getByLabelText("名称"), "Second");
  await userEvent.type(screen.getByLabelText("别名"), "analyst");
  await userEvent.click(screen.getByRole("button", { name: "创建" }));

  expect(await screen.findByText("该别名已被占用，请换一个")).toBeInTheDocument();
  // Recoverable without retyping is the whole point of the backend fix: the
  // dialog stays, the values stay, and nothing is resent behind the user.
  expect(screen.getByLabelText("名称")).toHaveValue("Second");
  expect(screen.getByLabelText("别名")).toHaveValue("analyst");
  await waitFor(() => expect(attempts).toBe(1));
});

test("a refusal is shown as itself, never traded for another workspace", async () => {
  const scopes: (string | null)[] = [];
  server.use(
    http.get("/api/v1/agents", ({ request }) => {
      scopes.push(request.headers.get("X-Workspace-Id"));
      return HttpResponse.json(
        { code: "forbidden", detail: "You may not access this resource." },
        { status: 403 },
      );
    }),
  );

  renderAgents(OTHER_WORKSPACE);

  expect(await screen.findByText("没有权限访问该工作空间的内容")).toBeInTheDocument();
  expect(screen.queryByText("somewhere else")).not.toBeInTheDocument();
  // One address, one request, one answer. No second workspace is tried.
  expect(scopes).toEqual([OTHER_WORKSPACE]);
});

const EXAMPLE = {
  slug: "notes-tidier",
  name: "Notes tidier",
  summary: "Reads note files and writes one summary.md.",
};

const ENDPOINT = {
  id: "e1",
  name: "acme-gpt",
  model: "acme-large",
  context_window: 128000,
  max_output_tokens: 4096,
  usage_quality: "provider",
  context_accounting: "provider",
  tokenizer: null,
  status: "enabled",
};

test("an empty workspace is offered the example rather than a blank page", async () => {
  // §21's last wizard step. Somebody who has just finished setup has nothing
  // to look at and no idea what an Agent spec should contain; "create one"
  // with a name field is not an answer to that.
  server.use(
    http.get("/api/v1/agents", () => HttpResponse.json([])),
    http.get("/api/v1/agents/examples", () => HttpResponse.json([EXAMPLE])),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([ENDPOINT])),
  );

  renderAgents();

  expect(await screen.findByText(/Reads note files/)).toBeVisible();
});

test("the example is created against a model endpoint the deployment actually has", async () => {
  // The one thing that can be wrong here and look right: creating it against
  // no endpoint, or against a hardcoded one. Publishing would then fail on a
  // deployment where the ids differ — which is every deployment but ours.
  let sent: unknown = null;
  server.use(
    http.get("/api/v1/agents", () => HttpResponse.json([])),
    http.get("/api/v1/agents/examples", () => HttpResponse.json([EXAMPLE])),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([ENDPOINT])),
    http.post("/api/v1/agents/examples/notes-tidier", async ({ request }) => {
      sent = await request.json();
      return HttpResponse.json(
        { agent: agent({ id: "a9", name: "Notes tidier" }), version_id: "v1" },
        { status: 201 },
      );
    }),
  );

  renderAgents();
  await userEvent.click(await screen.findByRole("button", { name: /创建示例|Create example/i }));

  await waitFor(() => expect(sent).toEqual({ endpoint_id: "e1" }));
});

test("with no model endpoint configured, the example says what is missing first", async () => {
  // §21 configures a model alias before this step. Offering a button that
  // can only fail — with a message about an endpoint id — teaches nothing.
  server.use(
    http.get("/api/v1/agents", () => HttpResponse.json([])),
    http.get("/api/v1/agents/examples", () => HttpResponse.json([EXAMPLE])),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([])),
  );

  renderAgents();

  expect(await screen.findByText(/模型接入点|model endpoint/i)).toBeVisible();
  expect(screen.queryByRole("button", { name: /创建示例|Create example/i })).toBeNull();
});

test("空的工作空间先看到三步，第一步指向接模型并说明做没做", async () => {
  server.use(
    http.get(`/api/v1/agents`, () => HttpResponse.json([])),
    http.get("/api/v1/agents/examples", () => HttpResponse.json([])),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([])),
  );

  renderAgents();

  const step = await screen.findByRole("link", { name: t("onboardingStep1") });
  expect(step).toHaveAttribute("href", `/workspaces/${WORKSPACE}/settings#model-endpoints`);
  expect(screen.getByText(t("onboardingStep1Todo"))).toBeVisible();
});
