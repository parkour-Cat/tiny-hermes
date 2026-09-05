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
    transport: "webhook",
    long_connection_state: "not_applicable",
    long_connection_seen_at: null,
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

test("the form sends the secret's id, never a key and never its name", async () => {
  // §4.6: `管理元数据，不查看明文` — a field taking the key itself would put
  // plaintext in a request body, which migration 0037 exists to prevent.
  //
  // The **id**, not the name: `CredentialResolver` resolves a Secret by id
  // (or an environment-variable name), so a binding storing the display name
  // validated cleanly and then failed at the first real delivery with
  // `CredentialMissing`. Measured against a live tenant — the webhook
  // answered 500 while the console had reported the binding as fine.
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
    encrypt_key_ref: "s1",
    app_secret_ref: "s2",
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

test("an issuer can be registered by pasting its public key instead of a JWKS url", async () => {
  // 真机走查撞上的第一堵墙。后端一直接受两者之一（`end_user_routes.py` 的
  // `public_key` / `jwks_url`），`channel_issuers` 两列并存，e2e 用的正是公钥那条；
  // 而这个表单只给 JWKS 地址且必填。后果是自建 IdP、不对外发布 JWKS 端点的企业，
  // 管理员在界面上无路可走——平台支持它，控制台不让他做。
  const pem = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBg\n-----END PUBLIC KEY-----";
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
  await userEvent.click(await screen.findByRole("radio", { name: t("issuerKeyModePublicKey") }));
  await userEvent.type(await screen.findByLabelText(t("issuerPublicKey")), pem);
  await userEvent.type(
    await screen.findByLabelText(t("issuerOrigins")),
    "https://portal.example.com",
  );
  await userEvent.click(screen.getByRole("button", { name: t("saveName") }));

  await waitFor(() => expect(sent).not.toBeNull());
  // `toEqual` rather than a per-key check: the point is that the unused half
  // is *absent*, not present-and-empty. A `jwks_url: ""` would be stored as a
  // second verification material the platform would then try to fetch.
  expect(sent).toEqual({
    channel: "web",
    issuer: "https://sso.example.com",
    public_key: pem,
    allowed_origins: ["https://portal.example.com"],
  });
});

test("a binding shows whether it can reply at all", async () => {
  // Without this column an operator cannot tell a receive-only binding from
  // one wired to reply, and "the Agent answered but Feishu showed nothing"
  // has no visible cause on the page that is supposed to explain it.
  server.use(
    http.get("/api/v1/channel-bindings", () =>
      HttpResponse.json([binding({ app_secret_ref: null })]),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderChannels();

  expect(await screen.findByText(t("channelReceiveOnly"))).toBeVisible();
});

test("an existing binding can be given the app secret it was made without", async () => {
  // The gap that made the whole reply path unusable: one binding per
  // (workspace, channel, agent), a constraint `disable` does not release,
  // so a binding created before outbound existed could never acquire a
  // secret and could never be replaced.
  let patched: Record<string, unknown> | null = null;
  server.use(
    http.get("/api/v1/channel-bindings", () =>
      HttpResponse.json([binding({ app_secret_ref: null })]),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
    http.get("/api/v1/secrets", () =>
      HttpResponse.json([
        { id: "s2", name: "feishu-app-secret", scope: "workspace", status: "active" },
      ]),
    ),
    http.patch("/api/v1/channel-bindings/b1", async ({ request }) => {
      patched = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(binding({ app_secret_ref: "s2" }));
    }),
  );

  renderChannels();
  await userEvent.click(await screen.findByRole("button", { name: t("channelEdit") }));
  await userEvent.click(await screen.findByLabelText(t("channelAppSecretRef")));
  await userEvent.click(await screen.findByTitle("feishu-app-secret"));
  await userEvent.click(screen.getByRole("button", { name: t("channelEditConfirm") }));

  await waitFor(() => expect(patched).not.toBeNull());
  // The id, and only the field that was changed. Sending the whole form
  // would resubmit `encrypt_key_ref` on every edit, and a stale value there
  // breaks inbound while fixing outbound.
  expect(patched).toEqual({ app_secret_ref: "s2" });
});

test("a binding shows which transport it receives on", async () => {
  // The switch that turns this on lives here. A console that could not show
  // the current value would make switching it a blind click.
  server.use(
    http.get("/api/v1/channel-bindings", () => HttpResponse.json([binding()])),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderChannels();

  expect(await screen.findByText(t("channelTransportWebhook"))).toBeVisible();
});

test("switching a binding to the long connection warns that the scheduler needs restarting", async () => {
  // The platform does not hot-reload transports (a deliberate choice, not a
  // gap) — a person who switches this and does not see the warning will
  // believe it took effect immediately, and messages will simply stop
  // arriving with no visible cause. This assertion is the only thing that
  // stands between switching and that failure.
  let patched: Record<string, unknown> | null = null;
  server.use(
    // `app_secret_ref` explicit rather than left off the fixture: the real
    // API always returns the field (null for receive-only), and leaving it
    // `undefined` here would make the edit dialog's own diffing see a
    // change that was never made. It is *set* rather than null because a
    // long connection is opened with the app id and app secret — a
    // receive-only binding cannot switch at all (the test below).
    http.get("/api/v1/channel-bindings", () =>
      HttpResponse.json([binding({ app_secret_ref: "s1" })]),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
    http.get("/api/v1/secrets", () =>
      HttpResponse.json([
        { id: "s1", name: "feishu-encrypt-key", scope: "workspace", status: "active" },
      ]),
    ),
    http.patch("/api/v1/channel-bindings/b1", async ({ request }) => {
      patched = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(binding({ transport: "long_connection" }));
    }),
  );

  renderChannels();
  await userEvent.click(await screen.findByRole("button", { name: t("channelEdit") }));
  await userEvent.click(await screen.findByLabelText(t("channelTransport")));
  await userEvent.click(await screen.findByTitle(t("channelTransportLongConnection")));
  await userEvent.click(screen.getByRole("button", { name: t("channelEditConfirm") }));

  await waitFor(() => expect(patched).not.toBeNull());
  expect(patched).toEqual({ transport: "long_connection" });
  expect(await screen.findByText(t("channelTransportRestartHint"))).toBeVisible();
});

test("a long connection nobody has ever connected still says a restart is owed", async () => {
  // Half of this task's premise is "you can see that it is on the long
  // connection", and until this test nothing rendered that value: both other
  // GETs answer `webhook`, so the long-connection branch of the column was
  // never executed.
  //
  // The restart note is on the row rather than only in the post-save Alert
  // because that Alert is one dismissible boolean in component state —
  // close it, reload, or navigate away and the console no longer records
  // anywhere that a restart is still owed. This one is derived from the
  // data, so it is there for whoever looks, whenever they look.
  server.use(
    http.get("/api/v1/channel-bindings", () =>
      HttpResponse.json([
        binding({ transport: "long_connection", long_connection_state: "never" }),
      ]),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderChannels();

  expect(await screen.findByText(t("channelTransportLongConnection"))).toBeVisible();
  expect(await screen.findByText(t("channelTransportRestartRequired"))).toBeVisible();
});

test("a long connection that is up does not tell anyone to restart the scheduler", async () => {
  // 这句提示原来是无条件显示的，因为当时页面没有任何办法分辨「刚切过来、
  // 还欠一次重启」和「切过来几个月、早就重启过了」——那一列的注释当时写着
  // 「Whoever wants the note to mean "still owed" has to give the API
  // something to say it with」。心跳就是那个东西。
  //
  // 有一拍活着的心跳，意味着 scheduler 已经在跑这个绑定了，重启不欠着。
  // 继续显示它是噪音；而 2026-09-04 走查里它更糟——它紧挨着红色的「已断开」，
  // 读起来像在说「重启就能修好」。那一次碰巧是对的，下一次连接因为别的原因
  // 断了，它就把人指向一个没用的操作。
  server.use(
    http.get("/api/v1/channel-bindings", () =>
      HttpResponse.json([
        binding({
          transport: "long_connection",
          long_connection_state: "connected",
          long_connection_seen_at: "2026-09-04T05:29:08Z",
        }),
      ]),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderChannels();

  expect(await screen.findByText(t("channelConnectionConnected"))).toBeVisible();
  expect(screen.queryByText(t("channelTransportRestartRequired"))).toBeNull();
});

test("a long connection that went stale does not tell anyone to restart the scheduler either", async () => {
  // 断开的原因不一定是「欠一次重启」——网络断了、凭据过期了都会走到这里。
  // 那一格已经说了「已断开」，旁边再挂一句重启提示只会把人指向一个可能没用
  // 的操作。
  server.use(
    http.get("/api/v1/channel-bindings", () =>
      HttpResponse.json([
        binding({
          transport: "long_connection",
          long_connection_state: "stale",
          long_connection_seen_at: "2026-09-04T05:20:31Z",
        }),
      ]),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderChannels();

  expect(await screen.findByText(t("channelConnectionStale"))).toBeVisible();
  expect(screen.queryByText(t("channelTransportRestartRequired"))).toBeNull();
});

test("a long connection that stopped being seen is shown as disconnected, not just as its transport", async () => {
  // 这一列原来只显示存的值。2026-09-03 一根绑定的 socket 死了十个小时，
  // 页面整晚写着「长连接」，而发现它的方式是有人发消息发不出去 —— 那一列
  // 自己的注释当时就承认了「carries the stored transport and nothing about
  // the running scheduler」。
  //
  // 判据是**页面上出现了「已断开」**，不是响应体里有那个字段：后端交出来了
  // 而界面没画，正是这个仓库反复栽的那一种。
  server.use(
    http.get("/api/v1/channel-bindings", () =>
      HttpResponse.json([
        binding({
          transport: "long_connection",
          long_connection_state: "stale",
          long_connection_seen_at: "2026-09-03T15:53:00Z",
        }),
      ]),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderChannels();

  expect(await screen.findByText(t("channelConnectionStale"))).toBeVisible();
  // 配置照旧要看得见：这一格是加在它旁边的，不是替掉它。两件事分开显示，
  // 因为「配成长连接」和「此刻连着」的修法完全不同。
  expect(await screen.findByText(t("channelTransportLongConnection"))).toBeVisible();
});

test("a long connection that is up says so", async () => {
  server.use(
    http.get("/api/v1/channel-bindings", () =>
      HttpResponse.json([
        binding({
          transport: "long_connection",
          long_connection_state: "connected",
          long_connection_seen_at: "2026-09-04T00:00:00Z",
        }),
      ]),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderChannels();

  expect(await screen.findByText(t("channelConnectionConnected"))).toBeVisible();
});

test("a transport this console has no wording for is shown as itself, not as Webhook", async () => {
  // The column's entire job is to say what the stored value is. Rendering
  // everything that is not `long_connection` as "Webhook" makes an unknown
  // value — a newer server, a hand-edited row — indistinguishable from the
  // one state a reader would never investigate.
  server.use(
    http.get("/api/v1/channel-bindings", () =>
      HttpResponse.json([binding({ transport: "carrier_pigeon" })]),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );

  renderChannels();

  expect(await screen.findByText("carrier_pigeon")).toBeVisible();
  expect(screen.queryByText(t("channelTransportWebhook"))).toBeNull();
});

test("a receive-only binding cannot be switched to the long connection", async () => {
  // The scheduler skips a `long_connection` binding with no app id or app
  // secret reference — it needs both to open the WebSocket — and all it
  // leaves behind is one log line. Saving the switch would give a console
  // that reads "long connection", a hint telling the administrator to
  // restart the scheduler, and then no messages, ever. The API refuses it
  // too (it is public), but being refused after the fact does not tell
  // anyone *why*: this says it where the choice is made.
  server.use(
    http.get("/api/v1/channel-bindings", () =>
      HttpResponse.json([binding({ app_secret_ref: null })]),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
    http.get("/api/v1/secrets", () =>
      HttpResponse.json([
        { id: "s1", name: "feishu-encrypt-key", scope: "workspace", status: "active" },
      ]),
    ),
  );

  renderChannels();
  await userEvent.click(await screen.findByRole("button", { name: t("channelEdit") }));
  await userEvent.click(await screen.findByLabelText(t("channelTransport")));

  expect(await screen.findByTitle(t("channelTransportLongConnection"))).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  // Not vacuous: the test above opens the same select on a binding that
  // *has* an app secret and clicks this very option through to a PATCH, so
  // "disabled" here is about this row and not about the control.
  expect(await screen.findByText(t("channelTransportNeedsCredentials"))).toBeVisible();
});

test("新建和编辑用的是同一套字段定义", async () => {
  // 三个弹窗里抄三遍的后果是：改了其中一处，另外两处不知道。
  server.use(
    http.get("/api/v1/channel-bindings", () => HttpResponse.json([binding()])),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
    http.get("/api/v1/secrets", () =>
      HttpResponse.json([
        { id: "s1", name: "feishu-encrypt-key", scope: "workspace", status: "active" },
      ]),
    ),
  );

  renderChannels();
  await userEvent.click(await screen.findByRole("button", { name: t("bindChannel") }));
  const creating = screen.getAllByRole("textbox").map((input) => input.getAttribute("id"));
  const createLabels = [t("channelKeyRef"), t("channelAppId"), t("channelAppSecretRef")].map(
    (label) => screen.getByLabelText(label).getAttribute("id"),
  );
  await userEvent.click(screen.getByRole("button", { name: t("cancel") }));
  await userEvent.click(await screen.findByRole("button", { name: t("channelEdit") }));
  const editing = screen.getAllByRole("textbox").map((input) => input.getAttribute("id"));
  const editLabels = [t("channelKeyRef"), t("channelAppId"), t("channelAppSecretRef")].map(
    (label) => screen.getByLabelText(label).getAttribute("id"),
  );

  expect(editing).toEqual(creating);
  expect(editLabels).toEqual(createLabels);
});

test("加密密钥这一列不整列显示 UUID", async () => {
  // 一个完整 UUID 折成两行占掉整屏最宽的一格，而那个值几乎没有人会去读。
  server.use(
    http.get("/api/v1/channel-bindings", () =>
      HttpResponse.json([binding({ encrypt_key_ref: "2361d9ea-6591-4960-8c67-07fd26c5c38e" })]),
    ),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );
  renderChannels();
  const cell = await screen.findByTitle("2361d9ea-6591-4960-8c67-07fd26c5c38e");
  expect(cell.textContent).not.toContain("07fd26c5c38e");
});

test("配置和状态各占一列", async () => {
  // 「接入方式」原来一格里同时放着配置标签、状态标签和一句灰字提示。
  server.use(
    http.get("/api/v1/channel-bindings", () => HttpResponse.json([binding()])),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );
  renderChannels();
  expect(await screen.findByRole("columnheader", { name: t("channelTransport") })).toBeVisible();
  expect(await screen.findByRole("columnheader", { name: t("channelConnection") })).toBeVisible();
});

test("可回复是文字，不是彩色标签", async () => {
  // 彩色标签只留给会变的状态；绿色在同一张表里还表示「连接中」。
  server.use(
    http.get("/api/v1/channel-bindings", () => HttpResponse.json([binding({ app_secret_ref: "s2" })])),
    http.get("/api/v1/agents", () => HttpResponse.json(AGENTS)),
  );
  renderChannels();
  // The column header says the same word; the cell is the one inside a <td>.
  const cell = (await screen.findAllByText(t("channelCanReply"))).find(
    (node) => node.closest("td") !== null,
  );
  expect(cell).toBeDefined();
  expect(cell!.className).not.toContain("ant-tag");
});
