import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { ChannelsPage } from "./ChannelsPage";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const AGENT = "22222222-3333-4444-8555-666666666666";

function binding(overrides: object = {}) {
  return {
    id: "b1",
    channel: "feishu",
    agent_id: AGENT,
    status: "active",
    app_id: "cli_a1b2c3",
    encrypt_key_ref: "feishu-encrypt-key",
    created_by: "u1",
    created_at: "2026-08-22T00:00:00Z",
    ...overrides,
  };
}

function renderChannels(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/channels`]}>
          <Routes>
            <Route path="/workspaces/:workspaceId/channels" element={<ChannelsPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

const AGENTS = [
  { id: AGENT, name: "Support", alias: "support", status: "active", current_version_id: "v1", created_at: "2026-08-01T00:00:00Z" },
];

test("a bound channel says which Agent it publishes and where", async () => {
  server.use(
    http.get("/api/v1/channel-bindings", () => HttpResponse.json([binding()])),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderChannels();

  // The Agent by name, not by id: "which Agent did we publish into Feishu"
  // is the question, and a uuid does not answer it.
  expect(await screen.findByText("Support")).toBeVisible();
  expect(screen.getByText("cli_a1b2c3")).toBeVisible();
});

test("the form sends the secret's name, never a key", async () => {
  // §4.6: `管理元数据，不查看明文`. A field that took the key itself would
  // put plaintext in a request body and in this page's memory, which is the
  // one thing this row is designed to avoid — see migration 0037.
  let sent: Record<string, unknown> | null = null;
  server.use(
    http.get("/api/v1/channel-bindings", () => HttpResponse.json([])),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
    http.get("/api/v1/secrets", () =>
      HttpResponse.json([{ id: "s1", name: "feishu-encrypt-key", scope: "workspace", status: "active" }]),
    ),
    http.post("/api/v1/channel-bindings", async ({ request }) => {
      sent = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(binding(), { status: 201 });
    }),
  );

  renderChannels();
  await userEvent.click(await screen.findByRole("button", { name: /绑定渠道|Bind a channel/i }));
  await userEvent.click(await screen.findByLabelText(/Agent/));
  await userEvent.click(await screen.findByTitle("Support"));
  await userEvent.click(screen.getByLabelText(/加密密钥|Encrypt key/i));
  await userEvent.click(await screen.findByTitle("feishu-encrypt-key"));
  await userEvent.type(screen.getByLabelText(/应用 ID|App ID/i), "cli_zzz");
  await userEvent.click(screen.getByRole("button", { name: /^绑定$|^Bind$/ }));

  await waitFor(() => expect(sent).not.toBeNull());
  expect(sent).toEqual({
    channel: "feishu",
    agent_id: AGENT,
    app_id: "cli_zzz",
    encrypt_key_ref: "feishu-encrypt-key",
  });
});

test("with no secret stored, the empty page says what to make first", async () => {
  // A Feishu binding cannot exist without one — migration 0037's CHECK — so
  // a form offered against an empty key list produces a refusal the person
  // cannot act on from here. Said in the empty state, which is where an
  // administrator who has bound nothing is actually standing.
  server.use(
    http.get("/api/v1/channel-bindings", () => HttpResponse.json([])),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
    http.get("/api/v1/secrets", () => HttpResponse.json([])),
  );

  renderChannels();

  expect(await screen.findByText(/密钥|secret/i)).toBeVisible();
});

test("a disabled binding is still shown, and cannot be disabled twice", async () => {
  // Disabled rather than deleted, because `channel_events` is the record of
  // what this channel already delivered. A page that hid it would make a
  // channel that used to be open look like one that never was.
  server.use(
    http.get("/api/v1/channel-bindings", () =>
      HttpResponse.json([binding({ status: "disabled" })]),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderChannels();

  expect(await screen.findByText("cli_a1b2c3")).toBeVisible();
  expect(screen.queryByRole("button", { name: /停用|Disable/ })).toBeNull();
});

test("a viewer is told they may not look, not shown an empty page", async () => {
  // §4.6 gives a viewer `否` on channels. An empty list would say "this
  // workspace publishes nothing", which is a different and false statement.
  server.use(
    http.get("/api/v1/channel-bindings", () =>
      HttpResponse.json({ code: "forbidden", detail: "" }, { status: 403 }),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderChannels();

  expect(await screen.findByText(/没有权限|not allowed|forbidden/i)).toBeVisible();
});
