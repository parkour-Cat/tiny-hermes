import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { AgentsPage } from "./AgentsPage";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

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

  expect(await screen.findByText("Analyst")).toBeInTheDocument();
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
