import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { useMyRole } from "./useMyRole";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";

function wrap({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/agents`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/agents" element={children} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

test("reports the role the server gave", async () => {
  server.use(
    // Refuses without the workspace header, as the real route does
    // (`_require_path_matches_header`). The first real walk found every
    // grouped page blank because the header was missing and no test noticed.
    http.get("/api/v1/workspaces/:id/members/me", ({ request, params }) =>
      request.headers.get("X-Workspace-Id") === params.id
        ? HttpResponse.json({ role: "viewer" })
        : HttpResponse.json({ code: "workspace_required" }, { status: 400 }),
    ),
  );
  const { result } = renderHook(() => useMyRole(), { wrapper: wrap });
  await waitFor(() => expect(result.current.role).toBe("viewer"));
});

test("a refused answer is null, never a guessed role", async () => {
  // 猜一个角色的后果是画出一个这个人点不动的段。宁可少画。
  server.use(
    http.get("/api/v1/workspaces/:id/members/me", () => new HttpResponse(null, { status: 403 })),
  );
  const { result } = renderHook(() => useMyRole(), { wrapper: wrap });
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.role).toBeNull();
});
