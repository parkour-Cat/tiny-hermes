import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { ModelEndpointsPage } from "./ModelEndpointsPage";
import { AuthProvider } from "../auth/AuthProvider";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";
import { t } from "../i18n/zh-CN";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const ENDPOINT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

const SUMMARY = {
  id: ENDPOINT,
  name: "acme-gpt",
  model: "acme-large",
  context_window: 128000,
  max_output_tokens: 4096,
  usage_quality: "provider",
  context_accounting: "shared",
  tokenizer: null,
  status: "active",
};

function user(admin: boolean) {
  return {
    id: "u1",
    subject: "user@example.com",
    display_name: "User",
    status: "active",
    is_platform_admin: admin,
  };
}

function renderEndpoints(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/model-endpoints`]}>
          <AuthProvider>
            <Routes>
              <Route
                path="/workspaces/:workspaceId/model-endpoints"
                element={<ModelEndpointsPage />}
              />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

test("a platform administrator sees the base url and whether a credential exists", async () => {
  let details = 0;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user(true))),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([SUMMARY])),
    http.get(`/api/v1/model-endpoints/${ENDPOINT}`, () => {
      details += 1;
      return HttpResponse.json({
        ...SUMMARY,
        kind: "openai_compatible",
        base_url: "https://models.example.com/v1",
        credential_available: true,
      });
    }),
  );

  renderEndpoints();

  expect(await screen.findByText("https://models.example.com/v1")).toBeInTheDocument();
  expect(screen.getByText("凭证已配置")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "接入模型服务" })).toBeInTheDocument();
  await waitFor(() => expect(details).toBe(1));
});

test("the window is listed with how it is counted, not just how big it is", async () => {
  // Two endpoints of the same declared window hold different amounts of
  // conversation depending on this one word, so the number alone would be a
  // half-truth to whoever is choosing between them.
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user(false))),
    http.get("/api/v1/model-endpoints", () =>
      HttpResponse.json([{ ...SUMMARY, context_accounting: "separate" }]),
    ),
  );

  renderEndpoints();

  expect(await screen.findByText(/128000 token/)).toHaveTextContent("输入与输出分别计算");
});

test("everyone else lists the summary and never the base url", async () => {
  let details = 0;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user(false))),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([SUMMARY])),
    http.get(`/api/v1/model-endpoints/${ENDPOINT}`, () => {
      details += 1;
      return HttpResponse.json({
        ...SUMMARY,
        kind: "openai_compatible",
        base_url: "https://models.example.com/v1",
        credential_available: true,
      });
    }),
  );

  renderEndpoints();

  expect(await screen.findByText("acme-gpt")).toBeInTheDocument();
  expect(screen.queryByText("https://models.example.com/v1")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "接入模型服务" })).not.toBeInTheDocument();
  expect(details).toBe(0);
});

const PRICE = {
  id: "p1",
  endpoint_id: ENDPOINT,
  version_number: 2,
  currency: "USD",
  input_per_million: "3.00",
  output_per_million: "15.00",
  cached_input_per_million: null,
  free: false,
  effective_at: "2026-08-01T00:00:00Z",
  created_by: "u1",
  created_at: "2026-08-01T00:00:00Z",
};

test("the price in force is shown, because usage is money and nothing else showed it", async () => {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user(true))),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([SUMMARY])),
    http.get(`/api/v1/model-endpoints/${ENDPOINT}`, () =>
      HttpResponse.json({
        ...SUMMARY,
        kind: "openai_compatible",
        base_url: "https://models.example.com/v1",
        credential_available: true,
      }),
    ),
    http.get(`/api/v1/model-endpoints/${ENDPOINT}/pricing`, () => HttpResponse.json(PRICE)),
  );

  renderEndpoints();

  expect(await screen.findByText(/3\.00/)).toBeInTheDocument();
  expect(screen.getByText(/15\.00/)).toBeInTheDocument();
});

test("an endpoint with no price says so, rather than showing zero", async () => {
  // "Priced at nothing" and "not priced" are different states, and a zero
  // shown for the second makes every Run of this endpoint look free.
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user(true))),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([SUMMARY])),
    http.get(`/api/v1/model-endpoints/${ENDPOINT}`, () =>
      HttpResponse.json({
        ...SUMMARY,
        kind: "openai_compatible",
        base_url: "https://models.example.com/v1",
        credential_available: true,
      }),
    ),
    http.get(`/api/v1/model-endpoints/${ENDPOINT}/pricing`, () =>
      HttpResponse.json({ code: "pricing_not_set", detail: "" }, { status: 404 }),
    ),
  );

  renderEndpoints();

  expect(await screen.findByText(t("pricingUnset"))).toBeInTheDocument();
});

test("setting a price sends decimal strings, never numbers", async () => {
  // Money as a float is how a rate becomes 3.0000000000000004. The API
  // takes text on purpose and the console must not undo that.
  let sent: Record<string, unknown> | null = null;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user(true))),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([SUMMARY])),
    http.get(`/api/v1/model-endpoints/${ENDPOINT}`, () =>
      HttpResponse.json({
        ...SUMMARY,
        kind: "openai_compatible",
        base_url: "https://models.example.com/v1",
        credential_available: true,
      }),
    ),
    http.get(`/api/v1/model-endpoints/${ENDPOINT}/pricing`, () =>
      HttpResponse.json({ code: "pricing_not_set", detail: "" }, { status: 404 }),
    ),
    http.post(`/api/v1/model-endpoints/${ENDPOINT}/pricing`, async ({ request }) => {
      sent = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(PRICE, { status: 201 });
    }),
  );

  renderEndpoints();
  await userEvent.click(await screen.findByRole("button", { name: t("setPricing") }));
  await userEvent.type(await screen.findByLabelText(t("pricingInput")), "3.00");
  await userEvent.type(await screen.findByLabelText(t("pricingOutput")), "15.00");
  await userEvent.click(screen.getByRole("button", { name: t("saveName") }));

  await waitFor(() => expect(sent).not.toBeNull());
  expect(sent).toEqual({
    currency: "USD",
    input_per_million: "3.00",
    output_per_million: "15.00",
  });
  expect(typeof sent!.input_per_million).toBe("string");
});

test("the credential is chosen from stored secrets, not typed as a uuid", async () => {
  // The field was a bare text box labelled 「凭证环境变量」 that also — with
  // nothing saying so — accepted a Secret's **UUID**. Not its name: the name
  // is what the channel binding form takes, so one console had two shapes of
  // reference and one of them was undiscoverable.
  //
  // The channel form's own comment already argued this: a free-text
  // reference is how you point at a secret that does not exist and find out
  // hours later, somewhere nobody is watching.
  let sent: Record<string, unknown> | null = null;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user(true))),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([])),
    // The stub refuses without `X-Workspace-Id`, because the real route
    // does (`require_workspace_id`). A stub that answered regardless is how
    // the first version of this picker shipped with an empty dropdown and a
    // green test: the page asked without the header and was refused, and
    // nothing in the suite could tell.
    http.get("/api/v1/secrets", ({ request }) => {
      if (request.headers.get("X-Workspace-Id") === null) {
        return HttpResponse.json({ code: "workspace_required", detail: "" }, { status: 400 });
      }
      return HttpResponse.json([
        { id: "11111111-2222-4333-8444-555555555555", name: "openai-api-key", scope: "workspace", status: "active" },
      ]);
    }),
    http.post("/api/v1/model-endpoints", async ({ request }) => {
      sent = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json({ ...SUMMARY, kind: "openai_compatible", base_url: "https://x/v1", credential_available: true }, { status: 201 });
    }),
  );

  renderEndpoints();

  await userEvent.type(await screen.findByLabelText(t("endpointName")), "main");
  await userEvent.type(screen.getByLabelText(t("endpointBaseUrl")), "https://api.openai.com/v1");
  await userEvent.type(screen.getByLabelText(t("endpointModel")), "gpt-4o-mini");
  // The secret is picked by the name a person recognises...
  await userEvent.click(screen.getByLabelText(t("endpointCredentialRef")));
  // The option shows the scope beside the name, so an operator can tell two
  // secrets with the same name apart.
  await userEvent.click(await screen.findByTitle("openai-api-key · workspace"));
  await userEvent.click(screen.getByRole("button", { name: t("registerEndpoint") }));

  await waitFor(() => expect(sent).not.toBeNull());
  // ...and what crosses the wire is the id the resolver actually accepts.
  expect(sent!.credential_ref).toBe("11111111-2222-4333-8444-555555555555");
});

test("an endpoint can be declared as accepting images", async () => {
  // Declared, never guessed from the model name. DeepSeek's vision support
  // is a different model id from its text one, so a name-sniffing check
  // would go silently wrong the next time a vendor renames anything — and
  // there has to be somewhere for an administrator to say it.
  let sent: Record<string, unknown> | null = null;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user(true))),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([])),
    http.get("/api/v1/secrets", () =>
      HttpResponse.json([
        {
          id: "11111111-2222-4333-8444-555555555555",
          name: "openai-api-key",
          scope: "workspace",
          status: "active",
        },
      ]),
    ),
    http.post("/api/v1/model-endpoints", async ({ request }) => {
      sent = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(
        {
          ...SUMMARY,
          kind: "openai_compatible",
          base_url: "https://x/v1",
          credential_available: true,
        },
        { status: 201 },
      );
    }),
  );

  renderEndpoints();

  await userEvent.type(await screen.findByLabelText(t("endpointName")), "vision");
  await userEvent.type(screen.getByLabelText(t("endpointBaseUrl")), "https://api.deepseek.com/v1");
  await userEvent.type(
    screen.getByLabelText(t("endpointModel")),
    "deepseek-v4-flash-vision-exp",
  );
  await userEvent.click(screen.getByLabelText(t("endpointCredentialRef")));
  await userEvent.click(await screen.findByTitle("openai-api-key · workspace"));
  await userEvent.click(screen.getByLabelText(t("endpointAcceptsImages")));
  await userEvent.click(screen.getByRole("button", { name: t("registerEndpoint") }));

  await waitFor(() => expect(sent).not.toBeNull());
  expect(sent!.accepts_images).toBe(true);
});

test("an endpoint is text-only unless somebody says otherwise", async () => {
  // The default that matters: every endpoint registered before this field
  // existed is text-only, and a form that silently sent `true` would send
  // images to endpoints that cannot read them.
  let sent: Record<string, unknown> | null = null;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user(true))),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json([])),
    http.get("/api/v1/secrets", () =>
      HttpResponse.json([
        {
          id: "11111111-2222-4333-8444-555555555555",
          name: "openai-api-key",
          scope: "workspace",
          status: "active",
        },
      ]),
    ),
    http.post("/api/v1/model-endpoints", async ({ request }) => {
      sent = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(
        {
          ...SUMMARY,
          kind: "openai_compatible",
          base_url: "https://x/v1",
          credential_available: true,
        },
        { status: 201 },
      );
    }),
  );

  renderEndpoints();

  await userEvent.type(await screen.findByLabelText(t("endpointName")), "plain");
  await userEvent.type(screen.getByLabelText(t("endpointBaseUrl")), "https://api.deepseek.com/v1");
  await userEvent.type(screen.getByLabelText(t("endpointModel")), "deepseek-v4-flash");
  await userEvent.click(screen.getByLabelText(t("endpointCredentialRef")));
  await userEvent.click(await screen.findByTitle("openai-api-key · workspace"));
  await userEvent.click(screen.getByRole("button", { name: t("registerEndpoint") }));

  await waitFor(() => expect(sent).not.toBeNull());
  expect(sent!.accepts_images).toBe(false);
});

test("an endpoint's image declaration can be corrected after the fact", async () => {
  // Registered with the switch off, and no way to say otherwise: the page
  // offered 测试连接 / 设定价格 / 停用 and nothing else. Somebody built two
  // vision endpoints and could not tell the platform either of them took
  // images.
  let sent: Record<string, unknown> | null = null;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user(true))),
    http.get("/api/v1/model-endpoints", () =>
      HttpResponse.json([
        {
          ...SUMMARY,
          id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
          name: "vision",
          model: "deepseek-v4-flash-vision-exp",
          accepts_images: false,
        },
      ]),
    ),
    http.patch("/api/v1/model-endpoints/:id", async ({ request }) => {
      sent = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json({ ...SUMMARY, accepts_images: true });
    }),
  );

  renderEndpoints();

  await userEvent.click(await screen.findByRole("button", { name: t("endpointAcceptsImages") }));

  await waitFor(() => expect(sent).not.toBeNull());
  expect(sent!.accepts_images).toBe(true);
  // Only that field. A PATCH also naming `status` would disable the
  // endpoint as a side effect of correcting a capability.
  expect(sent).not.toHaveProperty("status");
});

test("an endpoint's window can be widened after the fact, and nothing else moves", async () => {
  // The reason the control exists: a Run paused at context_overflow could
  // only be recovered by a database session, because no page and no route
  // changed a window. The PATCH names the two window facts and never status.
  let sent: Record<string, unknown> | null = null;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user(true))),
    http.get("/api/v1/model-endpoints", () =>
      HttpResponse.json([{ ...SUMMARY, accepts_images: false }]),
    ),
    http.patch("/api/v1/model-endpoints/:id", async ({ request }) => {
      sent = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json({ ...SUMMARY, ...sent, accepts_images: false });
    }),
  );

  renderEndpoints();
  await userEvent.click(await screen.findByRole("button", { name: t("adjustWindow") }));

  const dialog = await screen.findByRole("dialog");
  const windowField = within(dialog).getByLabelText(t("endpointContextWindow"));
  expect(windowField).toHaveValue("128000");
  await userEvent.clear(windowField);
  await userEvent.type(windowField, "512000");
  await userEvent.click(within(dialog).getByRole("button", { name: t("saveName") }));

  await waitFor(() => expect(sent).not.toBeNull());
  expect(sent).toEqual({ context_window: 512000, max_output_tokens: 4096 });
});
