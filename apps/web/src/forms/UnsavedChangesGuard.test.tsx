import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, Link, RouterProvider } from "react-router-dom";
import { expect, test } from "vitest";
import { TestTheme } from "../test/TestTheme";
import { UnsavedChangesGuard } from "./UnsavedChangesGuard";

test("unsaved edits survive a cancelled link and history navigation", async () => {
  const router = createMemoryRouter([
    { path: "/edit", element: <><UnsavedChangesGuard dirty /><input aria-label="Draft" defaultValue="Keep this" /><Link to="/away">Leave</Link></> },
    { path: "/away", element: <p>Destination</p> },
  ], { initialEntries: ["/away", "/edit"] });
  render(<TestTheme><RouterProvider router={router} /></TestTheme>);
  await userEvent.click(screen.getByRole("link", { name: "Leave" }));
  expect(await screen.findByRole("dialog")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "继续编辑" }));
  expect(screen.getByLabelText("Draft")).toHaveValue("Keep this");
  await router.navigate(-1);
  expect(await screen.findByRole("dialog")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "放弃修改并离开" }));
  await waitFor(() => expect(screen.getByText("Destination")).toBeVisible());
});
