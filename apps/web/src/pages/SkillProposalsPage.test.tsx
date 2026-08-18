import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { SkillProposalsPage } from "./SkillProposalsPage";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const PROPOSAL = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa";
const SKILL = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb";
const BASE = "cccccccc-3333-4333-8333-cccccccccccc";
const RUN = "dddddddd-4444-4444-8444-dddddddddddd";

const USER = {
  id: "u1",
  subject: "dev@example.com",
  display_name: "Dev",
  status: "active",
  is_platform_admin: false,
};

function proposal(overrides: object = {}) {
  return {
    id: PROPOSAL,
    skill_id: SKILL,
    base_version_id: BASE,
    name: "rollout",
    description: "How to drain a machine.",
    findings: [],
    origin: "agent",
    origin_run_id: RUN,
    status: "pending",
    approvable: true,
    created_by: "u1",
    created_at: "2026-08-18T00:00:00Z",
    decided_by: null,
    decided_at: null,
    ...overrides,
  };
}

function detail(overrides: object = {}) {
  return {
    ...proposal(overrides),
    files: [{ path: "SKILL.md", content: "---\nname: rollout\n---\n" }],
    diff: [
      {
        path: "SKILL.md",
        change: "changed",
        lines: [
          { kind: "context", text: "# Rollout" },
          { kind: "removed", text: "Drain it." },
          { kind: "added", text: "Check the dashboard first." },
        ],
        added_lines: 1,
        removed_lines: 1,
        truncated: false,
      },
    ],
  };
}

function renderProposals(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/skill-proposals`]}>
          <AuthProvider>
            <Routes>
              <Route
                path="/workspaces/:workspaceId/skill-proposals"
                element={<SkillProposalsPage />}
              />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("an agent's proposal says where it came from and shows its difference", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/skill-proposals", () => HttpResponse.json([proposal()])),
    http.get(`/api/v1/skill-proposals/${PROPOSAL}`, () => HttpResponse.json(detail())),
  );

  renderProposals();
  expect(await screen.findByText("Agent 提出")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "查看来源任务" })).toHaveAttribute(
    "href",
    `/workspaces/${WORKSPACE}/runs/${RUN}`,
  );
  await userEvent.click(screen.getByRole("button", { name: "差异" }));

  // Both sides of the change, so the decision is made against what would
  // actually be published rather than against a summary of it.
  expect(await screen.findByText(/Check the dashboard first\./)).toBeInTheDocument();
  expect(screen.getByText(/Drain it\./)).toBeInTheDocument();
  expect(screen.getByText("+1 −1")).toBeInTheDocument();
});

test("approving publishes a version and says the bindings did not move", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  let decided = false;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/skill-proposals", () =>
      HttpResponse.json([proposal(decided ? { status: "approved", approvable: false } : {})]),
    ),
    http.get(`/api/v1/skill-proposals/${PROPOSAL}`, () => HttpResponse.json(detail())),
    http.post(`/api/v1/skill-proposals/${PROPOSAL}/approve`, () => {
      decided = true;
      return HttpResponse.json(
        {
          id: "eeeeeeee-5555-4555-8555-eeeeeeeeeeee",
          skill_id: SKILL,
          version_number: 2,
          content_hash: "b".repeat(64),
          name: "rollout",
          description: "How to drain a machine.",
          findings: [],
          source: "proposal",
          source_url: null,
          source_ref: PROPOSAL,
          status: "active",
          bindable: true,
          created_at: "2026-08-18T00:00:00Z",
        },
        { status: 201 },
      );
    }),
  );

  renderProposals();
  await userEvent.click(await screen.findByRole("button", { name: "批准并发布新版本" }));

  expect(
    await screen.findByText("已发布版本 2。已发布的 Agent 的绑定没有改变。"),
  ).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText("已批准")).toBeInTheDocument());
});

test("a proposal the scan blocked has no approve control at all", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/skill-proposals", () =>
      HttpResponse.json([
        proposal({
          approvable: false,
          findings: [
            {
              code: "credential_material",
              severity: "blocking",
              path: "keys.md",
              detail: "an AWS access key id",
            },
          ],
        }),
      ]),
    ),
  );

  renderProposals();

  // Absent, not disabled — and the finding that stopped it is named, because
  // "cannot approve" alone sends the reader looking for a permission problem.
  expect(await screen.findByText(/静态扫描拦下了这条提案/)).toBeInTheDocument();
  expect(screen.getByText(/keys\.md: an AWS access key id/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "批准并发布新版本" })).not.toBeInTheDocument();
  // Rejecting is still available: a blocked proposal is one somebody should be
  // able to close.
  expect(screen.getByRole("button", { name: "拒绝" })).toBeInTheDocument();
});

test("rejecting warns that it produces nothing, then ends the proposal", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  let decided = false;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/skill-proposals", () =>
      HttpResponse.json([proposal(decided ? { status: "rejected", approvable: false } : {})]),
    ),
    http.post(`/api/v1/skill-proposals/${PROPOSAL}/reject`, () => {
      decided = true;
      return HttpResponse.json(proposal({ status: "rejected", approvable: false }));
    }),
  );

  renderProposals();
  await userEvent.click(await screen.findByRole("button", { name: "拒绝" }));

  expect(await screen.findByText("拒绝之后这条提案就结束了，不会产生任何版本。")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "确定" }));
  expect(await screen.findByText("已拒绝")).toBeInTheDocument();
});

test("an empty queue says so rather than showing an empty page", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/skill-proposals", () => HttpResponse.json([])),
  );

  renderProposals();

  expect(await screen.findByText("没有待审提案")).toBeInTheDocument();
});
