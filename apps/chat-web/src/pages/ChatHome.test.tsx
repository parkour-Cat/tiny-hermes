import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";
import { ChatHome } from "./ChatHome";
import { LocaleProvider } from "../i18n/locale";
import { server } from "../test/server";

test("service failures offer retry and do not send users back to enterprise login", async () => {
  let failed = true;
  server.use(http.get("/api/v1/end-user/agents", () => failed
    ? HttpResponse.json({ code: "request_failed" }, { status: 503 }) : HttpResponse.json([])));
  render(<LocaleProvider><MemoryRouter><ChatHome /></MemoryRouter></LocaleProvider>);
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  failed = false;
  await userEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByText("暂无可用 Agent")).toBeInTheDocument();
});

test("an authenticated empty list is distinct from missing authorization", async () => {
  server.use(http.get("/api/v1/end-user/agents", () => HttpResponse.json([])));
  render(<LocaleProvider><MemoryRouter><ChatHome /></MemoryRouter></LocaleProvider>);
  expect(await screen.findByText("暂无可用 Agent")).toBeInTheDocument();
});
