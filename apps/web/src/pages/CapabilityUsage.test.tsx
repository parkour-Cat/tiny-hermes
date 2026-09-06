import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";
import { CapabilityUsage } from "./CapabilityUsage";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

test("binding inspection distinguishes a published version from the changed draft", async () => {
  server.use(
    http.get("/api/v1/agents", () => HttpResponse.json([{ id: "agent1", name: "Order assistant", current_version_id: "published1" }])),
    http.get("/api/v1/agents/agent1/draft", () => HttpResponse.json({ spec: { skills: [{ skill_version_id: "new-version" }] } })),
    http.get("/api/v1/agents/agent1/versions/published1", () => HttpResponse.json({ spec: { skills: [{ skill_version_id: "old-version" }] } })),
  );
  render(<TestTheme><QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={["/workspaces/11111111-2222-4333-8444-555555555555"]}><Routes><Route path="/workspaces/:workspaceId" element={<CapabilityUsage kind="skills" versionId="old-version" />} /></Routes></MemoryRouter></QueryClientProvider></TestTheme>);
  await userEvent.click(screen.getByRole("button", { name: "查看使用情况" }));
  expect(await screen.findByRole("link", { name: "Order assistant" })).toHaveAttribute("href", "/workspaces/11111111-2222-4333-8444-555555555555/agents/agent1");
  await waitFor(() => expect(screen.getByText("已发布版本正在使用")).toBeVisible());
  expect(screen.queryByText("草稿选用了此版本")).toBeNull();
});
