import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";
import { OverviewPage } from "./OverviewPage";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

test("overview keeps failed task data separate from available Agent data", async () => {
  server.use(http.get("/api/v1/agents", () => HttpResponse.json([{ id: "a", current_version_id: "v" }])), http.get("/api/v1/runs", () => HttpResponse.json({ detail: "Tasks temporarily unavailable" }, { status: 503 })));
  render(<TestTheme><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={["/workspaces/11111111-2222-4333-8444-555555555555"]}><Routes><Route path="/workspaces/:workspaceId" element={<OverviewPage />} /></Routes></MemoryRouter></QueryClientProvider></TestTheme>);
  expect(await screen.findByText("1 / 1")).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "重试" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "任务" })).toHaveAttribute("href", "/workspaces/11111111-2222-4333-8444-555555555555/runs");
});
