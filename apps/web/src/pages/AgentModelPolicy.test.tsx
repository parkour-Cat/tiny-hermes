import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";

import { AgentDetailPage } from "./AgentDetailPage";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

/**
 * Choosing what an Agent talks to.
 *
 * The draft editor used to offer 模型场景 as though a deterministic scenario
 * were the only thing a model policy could be. Leaving it that way once real
 * endpoints exist would make the console misrepresent the platform, which is
 * the one thing phase 2C committed it would not do.
 */

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const AGENT = "22222222-3333-4444-8555-666666666666";
const ENDPOINT = "33333333-4444-4555-8666-777777777777";

const LIMITS = {
  max_execution_seconds: 600,
  max_elapsed_seconds: 3600,
  max_model_calls: 12,
  max_tool_calls: 7,
  max_derived_retries: 2,
};

const AGENT_ROW = {
  id: AGENT,
  name: "Analyst",
  alias: "analyst",
  status: "draft",
  current_version_id: null,
  created_at: "2026-08-10T00:00:00Z",
};

const ACTIVE_ENDPOINT = {
  id: ENDPOINT,
  name: "acme-gpt",
  model: "acme-large",
  context_window: 128000,
  max_output_tokens: 4096,
  usage_quality: "provider",
  status: "active",
};

function loaded(policy: Record<string, unknown>, endpoints: unknown[] = [ACTIVE_ENDPOINT]): void {
  server.use(
    http.get(`/api/v1/agents/${AGENT}`, () => HttpResponse.json(AGENT_ROW)),
    http.get(`/api/v1/agents/${AGENT}/draft`, () =>
      HttpResponse.json({
        agent_id: AGENT,
        revision: 3,
        spec: {
          schema_version: 1,
          personality: "You answer support questions.",
          model_policy: policy,
          tools: [],
          limits: LIMITS,
        },
        updated_at: "2026-08-10T01:00:00Z",
      }),
    ),
    http.get(`/api/v1/agents/${AGENT}/versions`, () => HttpResponse.json([])),
    http.get("/api/v1/model-endpoints", () => HttpResponse.json(endpoints)),
  );
}

function renderDetail(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <TestTheme>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE}/agents/${AGENT}`]}>
          <Routes>
            <Route
              path="/workspaces/:workspaceId/agents/:agentId"
              element={<AgentDetailPage />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </TestTheme>,
  );
}

/**
 * The select a Form.Item labelled `label` renders.
 *
 * By role and accessible name rather than by label text: Ant Design puts the
 * selected option's text in a `title` attribute, and `findByLabelText` falls
 * back to `title`, so a select showing "模型端点" answers to that name as well
 * as the field actually called it.
 */
function field(label: string): Promise<HTMLElement> {
  return screen.findByRole("combobox", { name: label });
}

/**
 * Picks a value from an Ant Design select.
 *
 * The option is looked up inside the dropdown, because the closed select
 * carries the same title as the option it is showing.
 */
async function choose(label: string, value: string): Promise<void> {
  await userEvent.click(await field(label));
  const option = await waitFor(() => {
    const found = document.querySelector<HTMLElement>(
      `.ant-select-item-option[title="${value}"]`,
    );
    if (found === null) {
      throw new Error(`no option titled ${value}`);
    }
    return found;
  });
  await userEvent.click(option);
}

function options(): string[] {
  return [...document.querySelectorAll<HTMLElement>(".ant-select-item-option")].map(
    (entry) => entry.getAttribute("title") ?? "",
  );
}

test("a deterministic draft shows the scenario and no endpoint", async () => {
  loaded({ provider: "deterministic", scenario: "continue_once" });
  renderDetail();

  expect(await field("模型场景")).toBeVisible();
  expect(screen.queryByRole("combobox", { name: "模型端点" })).toBeNull();
});

test("an endpoint draft shows the endpoint and no scenario", async () => {
  loaded({ provider: "openai_compatible", endpoint_id: ENDPOINT });
  renderDetail();

  expect(await field("模型端点")).toBeVisible();
  expect(screen.queryByRole("combobox", { name: "模型场景" })).toBeNull();
});

test("switching the provider swaps which field is offered", async () => {
  loaded({ provider: "deterministic", scenario: "complete" });
  renderDetail();
  await field("模型场景");

  await choose("模型提供方", "模型端点");

  expect(await field("模型端点")).toBeVisible();
  expect(screen.queryByRole("combobox", { name: "模型场景" })).toBeNull();
});

test("a disabled endpoint is not offered", async () => {
  loaded({ provider: "openai_compatible", endpoint_id: ENDPOINT }, [
    ACTIVE_ENDPOINT,
    { ...ACTIVE_ENDPOINT, id: "44444444-5555-4666-8777-888888888888", name: "retired-gpt", status: "disabled" },
  ]);
  renderDetail();

  await userEvent.click(await field("模型端点"));
  await waitFor(() => expect(options()).toContain("acme-gpt"));
  expect(options()).not.toContain("retired-gpt");
});

test("an empty endpoint list says so rather than offering an empty dropdown", async () => {
  loaded({ provider: "deterministic", scenario: "complete" }, []);
  renderDetail();
  await field("模型场景");

  await choose("模型提供方", "模型端点");

  // A dropdown with nothing in it looks like a loading bug. Saying that no
  // endpoint is registered points at the person who can fix it.
  expect(await screen.findByText("平台管理员尚未注册任何模型端点")).toBeVisible();
});

test("saving sends the policy the selection implies", async () => {
  loaded({ provider: "deterministic", scenario: "complete" });
  let sent: Record<string, unknown> | null = null;
  server.use(
    http.put(`/api/v1/agents/${AGENT}/draft`, async ({ request }) => {
      const payload = (await request.json()) as { spec: { model_policy: Record<string, unknown> } };
      sent = payload.spec.model_policy;
      return HttpResponse.json({
        agent_id: AGENT,
        revision: 4,
        spec: {
          schema_version: 1,
          personality: "You answer support questions.",
          model_policy: payload.spec.model_policy,
          tools: [],
          limits: LIMITS,
        },
        updated_at: "2026-08-10T02:00:00Z",
      });
    }),
  );
  renderDetail();
  await field("模型场景");

  await choose("模型提供方", "模型端点");
  await choose("模型端点", "acme-gpt");
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

  await waitFor(() => expect(sent).not.toBeNull());
  expect(sent).toEqual({ provider: "openai_compatible", endpoint_id: ENDPOINT });
});

test("a deterministic selection still sends a deterministic policy", async () => {
  loaded({ provider: "openai_compatible", endpoint_id: ENDPOINT });
  let sent: Record<string, unknown> | null = null;
  server.use(
    http.put(`/api/v1/agents/${AGENT}/draft`, async ({ request }) => {
      const payload = (await request.json()) as { spec: { model_policy: Record<string, unknown> } };
      sent = payload.spec.model_policy;
      return HttpResponse.json({
        agent_id: AGENT,
        revision: 4,
        spec: {
          schema_version: 1,
          personality: "You answer support questions.",
          model_policy: payload.spec.model_policy,
          tools: [],
          limits: LIMITS,
        },
        updated_at: "2026-08-10T02:00:00Z",
      });
    }),
  );
  renderDetail();
  await field("模型端点");

  await choose("模型提供方", "确定性场景");
  await choose("模型场景", "fail_replay_safe");
  await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

  await waitFor(() => expect(sent).not.toBeNull());
  expect(sent).toEqual({ provider: "deterministic", scenario: "fail_replay_safe" });
});
