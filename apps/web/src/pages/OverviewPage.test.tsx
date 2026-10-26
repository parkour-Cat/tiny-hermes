import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { OverviewPage } from "./OverviewPage";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";
import { WorkspacePermissions } from "../workspace/WorkspacePermissions";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const NOW = new Date("2026-10-26T15:00:00");

beforeEach(() => {
  vi.useFakeTimers({ now: NOW, shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
});

function run(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    agent_id: "agent-1",
    session_id: "s",
    agent_version_id: "v",
    status: "completed",
    failure_reason: null,
    input_preview: null,
    created_at: "2026-10-26T12:13:35+08:00",
    started_at: "2026-10-26T12:13:35+08:00",
    finished_at: "2026-10-26T12:14:39+08:00",
    ...overrides,
  };
}

type Runs = { today?: unknown[]; failed?: unknown[]; recent?: unknown[] };

/** Answers the three run listings the home page makes, told apart by their
 *  query, the way the real route tells them apart. */
function stack({ today = [], failed = [], recent = [] }: Runs = {}, seen: URLSearchParams[] = []) {
  server.use(
    http.get("/api/v1/auth/me", () =>
      HttpResponse.json({ id: "u", subject: "admin@example.com", display_name: "Admin", status: "active", is_platform_admin: false }),
    ),
    http.get("/api/v1/workspaces/:id/members/me", () => HttpResponse.json({ role: "workspace_admin" })),
    http.get("/api/v1/workspaces/:id/members", () => HttpResponse.json([{ id: "m1" }])),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([{ id: "e1" }])),
    http.get("/api/v1/channel-bindings", () => HttpResponse.json([{ id: "c1" }])),
    http.get("/api/v1/agents", () =>
      HttpResponse.json([{ id: "agent-1", name: "Notes tidier", alias: "notes-tidier", status: "active", current_version_id: "v7", created_at: "2026-10-01T00:00:00Z" }]),
    ),
    http.get("/api/v1/approvals", () => HttpResponse.json({ items: [], has_more: false })),
    http.get("/api/v1/skill-proposals", () => HttpResponse.json([])),
    http.get("/api/v1/memories/pending", () => HttpResponse.json([])),
    http.get("/api/v1/runs", ({ request }) => {
      const params = new URL(request.url).searchParams;
      seen.push(params);
      if (params.get("status") === "failed") return HttpResponse.json(failed);
      if (params.get("since") !== null) return HttpResponse.json(today);
      return HttpResponse.json(recent);
    }),
  );
}

function renderHome() {
  render(
    <TestTheme>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/overview`]}>
          <AuthProvider>
            <WorkspacePermissions role="workspace_admin">
              <Routes>
                <Route path="/workspaces/:workspaceId/overview" element={<OverviewPage />} />
              </Routes>
            </WorkspacePermissions>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("首页先列出要处理的事：最近一天失败的任务，一条一条能点进去", async () => {
  stack({
    failed: [run("run-f", { status: "failed", failure_reason: "model_rate_limited", input_preview: "把会议纪要整理成待办清单" })],
  });
  renderHome();
  const todo = await screen.findByRole("region", { name: "需要你处理" });
  const item = await within(todo).findByRole("link", { name: /把会议纪要整理成待办清单/ });
  expect(item).toHaveAttribute("href", `/workspaces/${WORKSPACE}/runs/run-f`);
  expect(within(todo).getByText(/model_rate_limited/)).toBeInTheDocument();
});

test("待办里有事时，首页带着数量指过去", async () => {
  stack();
  server.use(http.get("/api/v1/approvals", () => HttpResponse.json({ items: [{ id: "a" }, { id: "b" }], has_more: false })));
  renderHome();
  const todo = await screen.findByRole("region", { name: "需要你处理" });
  const inbox = await within(todo).findByRole("link", { name: /待办里有 2 件事/ });
  expect(inbox).toHaveAttribute("href", `/workspaces/${WORKSPACE}/inbox`);
});

test("没有要处理的事时直接说没有", async () => {
  stack();
  renderHome();
  const todo = await screen.findByRole("region", { name: "需要你处理" });
  expect(await within(todo).findByText("没有需要你处理的事")).toBeInTheDocument();
});

test("今天的数字只用今天零点以后的任务算，一个都不漏", async () => {
  // 不带 limit：带了就是用一页数据算一个看起来准确的错数。
  const seen: URLSearchParams[] = [];
  stack(
    {
      today: [
        run("a", { started_at: "2026-10-26T10:00:00+08:00", finished_at: "2026-10-26T10:00:10+08:00" }),
        run("b", { started_at: "2026-10-26T11:00:00+08:00", finished_at: "2026-10-26T11:00:20+08:00" }),
        run("c", { status: "failed", failure_reason: "x" }),
      ],
    },
    seen,
  );
  renderHome();
  const today = await screen.findByRole("region", { name: "今天" });
  expect(await within(today).findByText("3")).toBeInTheDocument();
  expect(within(today).getByText("67%")).toBeInTheDocument();
  expect(within(today).getByText("15 秒")).toBeInTheDocument();
  const todayQuery = seen.find((params) => params.get("since") !== null && params.get("status") === null)!;
  expect(new Date(todayQuery.get("since")!).getTime()).toBe(new Date("2026-10-26T00:00:00").getTime());
  expect(todayQuery.get("limit")).toBeNull();
});

test("最近的任务用对方说的第一句话当标题", async () => {
  stack({ recent: [run("run-1", { input_preview: "给我做一个有关 gemini 最新模型 argon 介绍的 PPT" })] });
  renderHome();
  const recent = await screen.findByRole("region", { name: "最近的任务" });
  const link = await within(recent).findByRole("link", { name: /给我做一个有关 gemini/ });
  expect(link).toHaveAttribute("href", `/workspaces/${WORKSPACE}/runs/run-1`);
  expect(within(recent).getByText(/Notes tidier/)).toBeInTheDocument();
  expect(within(recent).getByText("64 秒")).toBeInTheDocument();
});

test("任务读不到时那一块给出重试，Agent 那一块照常", async () => {
  stack();
  server.use(http.get("/api/v1/runs", () => HttpResponse.json({ detail: "Tasks temporarily unavailable" }, { status: 503 })));
  renderHome();
  const recent = await screen.findByRole("region", { name: "最近的任务" });
  expect(await within(recent).findByRole("button", { name: "重试" })).toBeInTheDocument();
  const agents = screen.getByRole("region", { name: "Agent" });
  expect(await within(agents).findByRole("link", { name: /Notes tidier/ })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/agents/agent-1`,
  );
});

test("配置进度只列没做完的那一步，全做完就不出现", async () => {
  stack();
  renderHome();
  const setup = await screen.findByRole("region", { name: "配置进度" });
  // 成员只有自己一个：邀请同事还没做。
  expect(await within(setup).findByRole("link", { name: /邀请同事/ })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/settings#members`,
  );
});
