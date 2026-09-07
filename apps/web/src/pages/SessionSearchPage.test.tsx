import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { SessionSearchPage } from "./SessionSearchPage";
import { t } from "../i18n/zh-CN";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";


function renderSearch(): void {
  render(<TestTheme><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/records`]}><Routes><Route path="/workspaces/:workspaceId/records" element={<SessionSearchPage />} /></Routes></MemoryRouter></QueryClientProvider></TestTheme>);
}
const HIT = {
  session_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  run_id: "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
  sequence: 4,
  role: "assistant",
  snippet: "…the rollout was moved to October…",
  shortened: true,
};

test("a search asks the server, and a shortened hit says it is shortened", async () => {
  // A snippet a reader does not know is partial gets read as the whole of a
  // message — which is the one way a search result can mislead somebody
  // reading a conversation they were not part of.
  let asked: URL | null = null;
  server.use(
    http.get("/api/v1/memories/pending", () => HttpResponse.json([])),
    http.get("/api/v1/memories/search", ({ request }) => {
      asked = new URL(request.url);
      return HttpResponse.json([HIT]);
    }),
  );

  renderSearch();
  await userEvent.type(await screen.findByLabelText(t("searchSessions")), "rollout");
  await userEvent.click(screen.getByRole("button", { name: t("searchRun") }));

  await waitFor(() => expect(asked).not.toBeNull());
  expect(asked!.searchParams.get("q")).toBe("rollout");
  expect(await screen.findByText(/rollout was moved/)).toBeVisible();
  expect(screen.getByText(t("searchShortened"))).toBeVisible();
});


test("a failed search reports the error without claiming there are no matches", async () => {
  server.use(http.get("/api/v1/memories/search", () => HttpResponse.json({ code: "forbidden" }, { status: 403 })));
  renderSearch();
  await userEvent.type(screen.getByLabelText(t("searchSessions")), "rollout");
  await userEvent.click(screen.getByRole("button", { name: t("searchRun") }));
  expect(await screen.findByRole("alert")).toBeVisible();
  expect(screen.queryByText(t("searchNoHits"))).not.toBeInTheDocument();
});
