import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { ChannelsPage } from "./ChannelsPage";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";
import { t } from "../i18n/zh-CN";

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
      HttpResponse.json([
        { id: "s1", name: "feishu-encrypt-key", scope: "workspace", status: "active" },
        { id: "s2", name: "feishu-app-secret", scope: "workspace", status: "active" },
      ]),
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
  await userEvent.click(screen.getByLabelText(/应用密钥|App secret/i));
  // Both selects list every stored secret, so the option title appears more
  // than once. Click the one in the dropdown that is actually open.
  const options = await screen.findAllByTitle("feishu-app-secret");
  const open = options.find(
    (node) => node.closest(".ant-select-dropdown:not(.ant-select-dropdown-hidden)") !== null,
  );
  expect(open).toBeDefined();
  await userEvent.click(open!);
  await userEvent.type(screen.getByLabelText(/应用 ID|App ID/i), "cli_zzz");
  await userEvent.click(screen.getByRole("button", { name: /^绑定$|^Bind$/ }));

  await waitFor(() => expect(sent).not.toBeNull());
  expect(sent).toEqual({
    channel: "feishu",
    agent_id: AGENT,
    app_id: "cli_zzz",
    encrypt_key_ref: "feishu-encrypt-key",
    app_secret_ref: "feishu-app-secret",
  });
});

test("the app secret is optional — a receive-only binding is allowed", async () => {
  // §929's drill needs one: it counts inbound events and never replies.
  // Leaving the app secret unset must not block binding.
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
  await userEvent.click(screen.getByRole("button", { name: /^绑定$|^Bind$/ }));

  await waitFor(() => expect(sent).not.toBeNull());
  expect(sent).not.toHaveProperty("app_secret_ref");
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

const ISSUER = {
  id: "i1",
  workspace_id: WORKSPACE,
  channel: "web",
  issuer: "https://sso.example.com",
  public_key: null,
  jwks_url: "https://sso.example.com/.well-known/jwks.json",
  allowed_origins: ["https://portal.example.com"],
  status: "active",
  created_by: "u1",
  created_at: "2026-08-20T00:00:00Z",
};

test("who may vouch for an end user is listed beside what they can talk to", async () => {
  // A binding says which Agent is published; an issuer says whose word this
  // platform takes for who a person is. Neither is usable without the other,
  // and until now only one of them had a page.
  server.use(
    http.get("/api/v1/channel-bindings", () => HttpResponse.json([binding()])),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
    http.get("/api/v1/channel-issuers", () => HttpResponse.json([ISSUER])),
  );

  renderChannels();

  expect(await screen.findByText("https://sso.example.com")).toBeVisible();
  // The origins are the embedding allow-list. Shown, because an origin
  // nobody remembers adding is how a portal stops working.
  expect(screen.getByText(/portal\.example\.com/)).toBeVisible();
});

test("registering an issuer sends the key reference shape the API takes", async () => {
  let sent: Record<string, unknown> | null = null;
  server.use(
    http.get("/api/v1/channel-bindings", () => HttpResponse.json([])),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
    http.get("/api/v1/secrets", () => HttpResponse.json([])),
    http.get("/api/v1/channel-issuers", () => HttpResponse.json([])),
    http.post("/api/v1/channel-issuers", async ({ request }) => {
      sent = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(ISSUER, { status: 201 });
    }),
  );

  renderChannels();
  await userEvent.click(await screen.findByRole("button", { name: t("registerIssuer") }));
  await userEvent.type(await screen.findByLabelText(t("issuerName")), "https://sso.example.com");
  await userEvent.type(
    await screen.findByLabelText(t("issuerJwksUrl")),
    "https://sso.example.com/.well-known/jwks.json",
  );
  await userEvent.type(
    await screen.findByLabelText(t("issuerOrigins")),
    "https://portal.example.com",
  );
  await userEvent.click(screen.getByRole("button", { name: t("saveName") }));

  await waitFor(() => expect(sent).not.toBeNull());
  expect(sent).toEqual({
    channel: "web",
    issuer: "https://sso.example.com",
    jwks_url: "https://sso.example.com/.well-known/jwks.json",
    // One per line, split here rather than sent as a blob: the API takes a
    // list and a comma inside an origin would otherwise split it in two.
    allowed_origins: ["https://portal.example.com"],
  });
});
