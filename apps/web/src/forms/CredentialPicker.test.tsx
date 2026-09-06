import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test, vi } from "vitest";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";
import { CredentialPicker } from "./CredentialPicker";

test("selects a saved credential by name and offers unclassified legacy credentials", async () => {
  const changed = vi.fn();
  server.use(http.get("/api/v1/secrets", () => HttpResponse.json([
    { id: "tool-key", name: "Order service", status: "active", purpose: "tool", scope: "workspace" },
    { id: "old-key", name: "Legacy credential", status: "active", scope: "workspace" },
    { id: "model-key", name: "Model only", status: "active", purpose: "model", scope: "platform" },
  ])));
  render(<TestTheme><QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={["/workspaces/11111111-2222-4333-8444-555555555555"]}><Routes><Route path="/workspaces/:workspaceId" element={<CredentialPicker purpose="tool" onChange={changed} />} /></Routes></MemoryRouter></QueryClientProvider></TestTheme>);
  await userEvent.click(screen.getByRole("combobox"));
  await userEvent.click(await screen.findByText("Order service · 工作空间"));
  expect(changed).toHaveBeenCalledWith("tool-key");
  expect(screen.queryByText("Model only · 平台")).toBeNull();
});

test("platform login only offers platform credentials and keeps the form open when adding", async () => {
  server.use(http.get("/api/v1/secrets", () => HttpResponse.json([
    { id: "local", name: "Space login", status: "active", purpose: "login", scope: "workspace" },
    { id: "global", name: "Global login", status: "active", purpose: "login", scope: "platform" },
  ])));
  render(<TestTheme><QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={["/workspaces/11111111-2222-4333-8444-555555555555"]}><Routes><Route path="/workspaces/:workspaceId" element={<CredentialPicker purpose="login" />} /></Routes></MemoryRouter></QueryClientProvider></TestTheme>);
  await userEvent.click(screen.getByRole("combobox"));
  await screen.findByText("Global login · 平台");
  expect(screen.queryByText("Space login · 工作空间")).toBeNull();
  expect(screen.getByRole("link", { name: "新建凭据" })).toHaveAttribute("target", "_blank");
});
