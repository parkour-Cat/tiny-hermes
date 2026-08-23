import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { SubjectDataPage } from "./SubjectDataPage";
import { t } from "../i18n/zh-CN";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const SUBJECT = "33333333-4444-4555-8666-777777777777";

function resolved(overrides: object = {}) {
  return {
    subject_id: SUBJECT,
    channel: "web",
    external_user_id: "alice@example.com",
    erased_at: null,
    first_seen_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function exported(memories: object[] = []) {
  return {
    subject_type: "end_user",
    subject_id: SUBJECT,
    workspace_id: WORKSPACE,
    memories,
    sessions: ["55555555-6666-4777-8888-999999999999"],
  };
}

function memory(overrides: object = {}) {
  return {
    id: "m1",
    workspace_id: WORKSPACE,
    agent_id: "22222222-3333-4444-8555-666666666666",
    kind: "private",
    status: "active",
    body: "They prefer mornings.",
    origin: "agent_proposal",
    created_at: "2026-08-02T00:00:00Z",
    updated_at: "2026-08-02T00:00:00Z",
    ...overrides,
  };
}

function renderSubjects(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/subjects`]}>
          <Routes>
            <Route path="/workspaces/:workspaceId/subjects" element={<SubjectDataPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

async function lookUp(): Promise<void> {
  await userEvent.type(
    await screen.findByLabelText(t("subjectExternalId")),
    "alice@example.com",
  );
  await userEvent.click(screen.getByRole("button", { name: t("subjectFind") }));
}

test("a request names a person, not a uuid", async () => {
  // How a data-rights request actually arrives: somebody writes in, named
  // the way the enterprise's directory names them. Every route that acts on
  // a subject takes the internal id, and nothing gave an administrator one.
  let asked: URL | null = null;
  server.use(
    http.get("/api/v1/subjects/lookup", ({ request }) => {
      asked = new URL(request.url);
      return HttpResponse.json(resolved());
    }),
    http.get(`/api/v1/subjects/${SUBJECT}/export`, () =>
      HttpResponse.json(exported([memory()])),
    ),
  );

  renderSubjects();
  await lookUp();

  await waitFor(() => expect(asked).not.toBeNull());
  expect(asked!.searchParams.get("external_user_id")).toBe("alice@example.com");
  expect(await screen.findByText("They prefer mornings.")).toBeVisible();
});

test("a name nobody here uses says so, rather than showing an empty person", async () => {
  // An empty memory list under a heading with their name would read as
  // "we hold nothing about this person" — which is a different statement
  // from "this person is not an end user here", and only one is true.
  server.use(
    http.get("/api/v1/subjects/lookup", () =>
      HttpResponse.json({ code: "subject_not_found", detail: "" }, { status: 404 }),
    ),
  );

  renderSubjects();
  await lookUp();

  expect(await screen.findByText(t("subjectNotFound"))).toBeVisible();
});

test("an already-erased subject is shown as erased, not as empty", async () => {
  // §344 keeps the row. A second request from the same person needs
  // "already erased, on this date" — an empty export cannot say that.
  server.use(
    http.get("/api/v1/subjects/lookup", () =>
      HttpResponse.json(resolved({ erased_at: "2026-08-10T09:00:00Z" })),
    ),
    http.get(`/api/v1/subjects/${SUBJECT}/export`, () => HttpResponse.json(exported())),
  );

  renderSubjects();
  await lookUp();

  expect(await screen.findByText(t("subjectAlreadyErased"))).toBeVisible();
  // And erasing again is not offered.
  expect(screen.queryByRole("button", { name: t("subjectErase") })).toBeNull();
});

test("erasing asks first and then reports what went", async () => {
  // The counts are the only thing that afterwards tells an erasure from one
  // that never ran — the same counts the audit line carries.
  server.use(
    http.get("/api/v1/subjects/lookup", () => HttpResponse.json(resolved())),
    http.get(`/api/v1/subjects/${SUBJECT}/export`, () =>
      HttpResponse.json(exported([memory()])),
    ),
    http.post(`/api/v1/subjects/${SUBJECT}/erase`, () =>
      HttpResponse.json({ memories: 3, sessions: 2, messages: 40, artifacts: 1 }),
    ),
  );

  renderSubjects();
  await lookUp();
  await userEvent.click(await screen.findByRole("button", { name: t("subjectErase") }));
  await userEvent.click(await screen.findByRole("button", { name: t("confirm") }));

  expect(await screen.findByText(/40/)).toBeVisible();
});

test("a memory can be corrected in place, and the correction is what is sent", async () => {
  let sent: unknown = null;
  server.use(
    http.get("/api/v1/subjects/lookup", () => HttpResponse.json(resolved())),
    http.get(`/api/v1/subjects/${SUBJECT}/export`, () =>
      HttpResponse.json(exported([memory()])),
    ),
    http.post("/api/v1/subjects/memories/m1/correct", async ({ request }) => {
      sent = await request.json();
      return HttpResponse.json(memory({ body: "They prefer afternoons." }));
    }),
  );

  renderSubjects();
  await lookUp();
  await userEvent.click(await screen.findByRole("button", { name: t("subjectCorrect") }));
  const field = await screen.findByLabelText(t("subjectCorrectedText"));
  await userEvent.clear(field);
  await userEvent.type(field, "They prefer afternoons.");
  await userEvent.click(screen.getByRole("button", { name: t("saveName") }));

  await waitFor(() => expect(sent).toEqual({ body: "They prefer afternoons." }));
});
