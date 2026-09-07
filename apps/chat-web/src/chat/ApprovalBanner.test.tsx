import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { ApprovalBanner } from "./ApprovalBanner";
import { LocaleProvider } from "../i18n/locale";

test("the user sees the real target and parameters before confirming", () => {
  render(<LocaleProvider><QueryClientProvider client={new QueryClient()}><ApprovalBanner approval={{
    id: "a1", run_id: "r1", approval_type: "user_confirmation", status: "pending",
    tool: "http.orders.create", document: { target: "https://example.com/orders", arguments: { sku: "blue-42", quantity: 2 } },
    required_permission: null, requested_by: "u1", expires_at: "2026-10-01T00:00:00Z",
  }} /></QueryClientProvider></LocaleProvider>);
  expect(screen.getByText("https://example.com/orders")).toBeVisible();
  expect(screen.getByText(JSON.stringify({ sku: "blue-42", quantity: 2 }, null, 2), { exact: true, collapseWhitespace: false })).toBeVisible();
});
