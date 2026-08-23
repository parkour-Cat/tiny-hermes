import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { SkillsPage } from "./SkillsPage";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const MINE = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa";
const THEIRS = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb";
const V1 = "cccccccc-3333-4333-8333-cccccccccccc";
const V2 = "dddddddd-4444-4444-8444-dddddddddddd";

const USER = {
  id: "u1",
  subject: "dev@example.com",
  display_name: "Dev",
  status: "active",
  is_platform_admin: false,
};

const SKILL_MD = ["---", "name: rollout", "description: How to drain a machine.", "---", ""].join(
  "\n",
);

function skill(id: string, name: string, scope: "workspace" | "platform", current: string | null) {
  return {
    id,
    scope,
    workspace_id: scope === "workspace" ? WORKSPACE : null,
    name,
    current_version_id: current,
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:00Z",
  };
}

function version(id: string, skillId: string, number: number, overrides: object = {}) {
  return {
    id,
    skill_id: skillId,
    version_number: number,
    content_hash: "a".repeat(64),
    name: "rollout",
    description: "How to drain a machine.",
    findings: [],
    source: "upload",
    source_url: null,
    source_ref: null,
    status: "active",
    bindable: true,
    created_at: "2026-08-18T00:00:00Z",
    ...overrides,
  };
}

function renderSkills(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/skills`]}>
          <AuthProvider>
            <Routes>
              <Route path="/workspaces/:workspaceId/skills" element={<SkillsPage />} />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("a platform skill is readable here and carries no controls", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/skills", () =>
      HttpResponse.json([
        skill(MINE, "rollout", "workspace", V1),
        skill(THEIRS, "house-style", "platform", V2),
      ]),
    ),
    http.get(`/api/v1/skills/${MINE}/versions`, () =>
      HttpResponse.json([version(V1, MINE, 1)]),
    ),
    http.get(`/api/v1/skills/${THEIRS}/versions`, () =>
      HttpResponse.json([version(V2, THEIRS, 1)]),
    ),
  );

  renderSkills();

  expect(await screen.findByText("rollout")).toBeInTheDocument();
  expect(screen.getByText("house-style")).toBeInTheDocument();
  // One withdraw button, belonging to this workspace's own skill. The platform
  // one renders without controls rather than with disabled ones.
  await waitFor(() => expect(screen.getAllByRole("button", { name: "停用" })).toHaveLength(1));
});

test("uploading a directory sends a file list, never an archive", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  let sent: { scope: string; files: { path: string; content: string }[] } | null = null;
  const catalog: ReturnType<typeof skill>[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/skills", () => HttpResponse.json(catalog)),
    http.get(`/api/v1/skills/${MINE}/versions`, () =>
      HttpResponse.json([version(V1, MINE, 1)]),
    ),
    http.post("/api/v1/skills", async ({ request }) => {
      sent = (await request.json()) as typeof sent;
      const created = skill(MINE, "rollout", "workspace", V1);
      catalog.push(created);
      return HttpResponse.json(created, { status: 201 });
    }),
  );

  renderSkills();
  await userEvent.upload(await screen.findByLabelText("选择文件"), [
    new File([SKILL_MD], "SKILL.md", { type: "text/markdown" }),
    new File(["Drain it slowly."], "reference.md", { type: "text/markdown" }),
  ]);

  await waitFor(() => expect(sent).not.toBeNull());
  const body = sent as unknown as { scope: string; files: { path: string; content: string }[] };
  expect(body.scope).toBe("workspace");
  expect(body.files.map((file) => file.path)).toEqual(["SKILL.md", "reference.md"]);
  expect(body.files[0]?.content).toContain("name: rollout");
  expect(await screen.findByText("rollout")).toBeInTheDocument();
});

test("importing the same content again says no version was created", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/skills", () => HttpResponse.json([skill(MINE, "rollout", "workspace", V1)])),
    http.get(`/api/v1/skills/${MINE}/versions`, () =>
      HttpResponse.json([
        version(V1, MINE, 1, { source: "git", source_url: "https://example.com/a.tar.gz" }),
      ]),
    ),
    // 200, not 201: this content was already a version.
    http.post(`/api/v1/skills/${MINE}/versions/import`, () =>
      HttpResponse.json(version(V1, MINE, 1, { source: "git" }), { status: 200 }),
    ),
  );

  renderSkills();
  const row = (await screen.findByText("rollout")).closest("article");
  // The re-import control appears only once the versions are loaded and one of
  // them says where it came from — an upload has nowhere to re-import from.
  await userEvent.click(
    await within(row as HTMLElement).findByRole("button", { name: "从 Git 导入" }),
  );

  expect(
    await screen.findByText("内容与现有版本相同，没有产生新版本。"),
  ).toBeInTheDocument();
});

test("withdrawing a version warns that bound agents keep running", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  let withdrew = false;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/skills", () => HttpResponse.json([skill(MINE, "rollout", "workspace", V1)])),
    http.get(`/api/v1/skills/${MINE}/versions`, () =>
      HttpResponse.json([version(V1, MINE, 1, { status: withdrew ? "withdrawn" : "active" })]),
    ),
    http.post(`/api/v1/skills/${MINE}/versions/${V1}/withdraw`, () => {
      withdrew = true;
      return HttpResponse.json(version(V1, MINE, 1, { status: "withdrawn" }));
    }),
  );

  renderSkills();
  await userEvent.click(await screen.findByRole("button", { name: "停用" }));

  // The warning is the point: the version stops being bindable, and nothing
  // that already binds it changes.
  expect(await screen.findByText(/已经绑定它的 Agent 不受影响/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "确定" }));
  expect(await screen.findByText("已停用")).toBeInTheDocument();
});

test("a blocked version is labelled and cannot be made the default", async () => {
  document.cookie = "tiny_hermes_csrf=token-value";
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(USER)),
    http.get("/api/v1/skills", () => HttpResponse.json([skill(MINE, "rollout", "workspace", V1)])),
    http.get(`/api/v1/skills/${MINE}/versions`, () =>
      HttpResponse.json([
        version(V1, MINE, 1),
        version(V2, MINE, 2, {
          bindable: false,
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

  renderSkills();

  expect(await screen.findByText("扫描未通过")).toBeInTheDocument();
  // Version 2 is not bindable, so it is not offered as a place for new
  // bindings to start. Version 1 already is the default, so it is not either.
  expect(screen.queryByRole("button", { name: "设为新绑定起点" })).not.toBeInTheDocument();
});
