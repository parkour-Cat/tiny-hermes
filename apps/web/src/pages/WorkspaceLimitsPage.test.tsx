import { WorkspacePermissions } from "../workspace/WorkspacePermissions";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { WorkspaceLimitsPage } from "./WorkspaceLimitsPage";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const URL = `/api/v1/workspaces/${WORKSPACE}/limits`;

function limits(values: Record<string, number | null> = {}) {
  return {
    values: {
      agent_max_execution_seconds: null,
      agent_max_model_calls: null,
      max_running_runs_per_caller: null,
      max_running_runs: null,
      ...values,
    },
    defaults: {
      agent_max_execution_seconds: 900,
      agent_max_model_calls: 20,
      max_running_runs_per_caller: 3,
      max_running_runs: null,
    },
    ceilings: {
      agent_max_execution_seconds: 7200,
      agent_max_model_calls: 200,
      max_running_runs_per_caller: 1000,
      max_running_runs: 10000,
    },
  };
}

function renderLimits(role: "workspace_admin" | "developer" = "workspace_admin"): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/settings`]}>
          <Routes>
            <Route
              path="/workspaces/:workspaceId/settings"
              element={<WorkspacePermissions role={role}><WorkspaceLimitsPage /></WorkspacePermissions>}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("an unset limit shows the platform default it falls back to", async () => {
  server.use(http.get(URL, () => HttpResponse.json(limits())));

  renderLimits();

  const execution = await screen.findByLabelText("单个任务最长执行时间（秒）");
  expect(execution).toHaveValue("");
  expect(execution).toHaveAttribute("placeholder", "默认 900");
  expect(screen.getByText("最多 7200")).toBeInTheDocument();
});

test("an administrator raises a limit and the page sends only numbers or null", async () => {
  const sent: unknown[] = [];
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.get(URL, () => HttpResponse.json(limits({ max_running_runs: 40 }))),
    http.put(URL, async ({ request }) => {
      sent.push(await request.json());
      return HttpResponse.json(limits({ agent_max_execution_seconds: 3600, max_running_runs: 40 }));
    }),
  );
  renderLimits();

  const execution = await screen.findByLabelText("单个任务最长执行时间（秒）");
  await userEvent.type(execution, "3600");
  await userEvent.click(screen.getByRole("button", { name: "保存" }));

  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0]).toEqual({
    agent_max_execution_seconds: 3600,
    agent_max_model_calls: null,
    max_running_runs_per_caller: null,
    max_running_runs: 40,
  });
});

test("a member who is not an administrator sees the limits and cannot change them", async () => {
  server.use(http.get(URL, () => HttpResponse.json(limits({ agent_max_model_calls: 60 }))));

  renderLimits("developer");

  const calls = await screen.findByLabelText("单个任务最多调用模型次数");
  expect(calls).toHaveValue("60");
  expect(calls).toBeDisabled();
  expect(screen.queryByRole("button", { name: "保存" })).not.toBeInTheDocument();
});
