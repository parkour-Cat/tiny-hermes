import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { expect, test, vi } from "vitest";
import { QuickModelConnect } from "./QuickModelConnect";
import { TestTheme } from "../test/TestTheme";
import { server } from "../test/server";

function setup() {
  const done = vi.fn();
  render(<TestTheme><QueryClientProvider client={new QueryClient()}><MemoryRouter>
    <QuickModelConnect workspaceId="workspace" secrets={[]} onConnected={done} onManual={() => undefined} />
  </MemoryRouter></QueryClientProvider></TestTheme>);
  return done;
}

test("address, key and a discovered model are enough; only save persists the key", async () => {
  const stored: unknown[] = [];
  const endpoints: Record<string, unknown>[] = [];
  server.use(
    http.post("/api/v1/model-endpoints/discover", async ({ request }) => {
      expect(await request.json()).toEqual({ base_url: "https://models.example.com/v1", api_key: "test-key" });
      return HttpResponse.json({ models: ["acme-large", "acme-small"] });
    }),
    http.post("/api/v1/secrets", async ({ request }) => {
      stored.push(await request.json()); return HttpResponse.json({ id: "saved-key" });
    }),
    http.post("/api/v1/model-endpoints", async ({ request }) => {
      endpoints.push(await request.json() as Record<string, unknown>);
      return HttpResponse.json({ id: "new-model", name: "acme-large" });
    }),
  );
  const done = setup();
  await userEvent.type(screen.getByLabelText("服务地址"), "https://models.example.com/v1");
  await userEvent.type(screen.getByLabelText("API Key"), "test-key");
  await userEvent.click(screen.getByRole("button", { name: "获取模型列表" }));
  await userEvent.click(await screen.findByRole("combobox", { name: "选择模型" }));
  await userEvent.click(await screen.findByText("acme-large", { selector: ".ant-select-item-option-content" }));
  expect(stored).toHaveLength(0);
  await userEvent.click(screen.getByRole("button", { name: "添加模型" }));
  await waitFor(() => expect(done).toHaveBeenCalledOnce());
  expect(stored).toHaveLength(1);
  expect(endpoints[0]).toMatchObject({ model: "acme-large", credential_ref: "saved-key", context_window: 8192, max_output_tokens: 1024 });
  expect(JSON.stringify(endpoints)).not.toContain("test-key");
});

test("changing the address discards models from the old connection", async () => {
  server.use(http.post("/api/v1/model-endpoints/discover", () => HttpResponse.json({ models: ["old-model"] })));
  setup();
  await userEvent.type(screen.getByLabelText("服务地址"), "https://old.example.com/v1");
  await userEvent.type(screen.getByLabelText("API Key"), "test-key");
  await userEvent.click(screen.getByRole("button", { name: "获取模型列表" }));
  expect(await screen.findByRole("combobox", { name: "选择模型" })).toBeVisible();
  await userEvent.type(screen.getByLabelText("服务地址"), "/changed");
  expect(screen.queryByRole("combobox", { name: "选择模型" })).toBeNull();
  expect(screen.queryByRole("button", { name: "添加模型" })).toBeNull();
});

test("a failed model registration reuses the saved credential on retry", async () => {
  let keys = 0; let saves = 0;
  server.use(
    http.post("/api/v1/model-endpoints/discover", () => HttpResponse.json({ models: ["acme"] })),
    http.post("/api/v1/secrets", () => { keys++; return HttpResponse.json({ id: "saved-key" }); }),
    http.post("/api/v1/model-endpoints", () => {
      saves++;
      return saves === 1 ? HttpResponse.json({ code: "network_failed" }, { status: 503 }) : HttpResponse.json({ id: "ok" });
    }),
  );
  const done = setup();
  await userEvent.type(screen.getByLabelText("服务地址"), "https://models.example.com/v1");
  await userEvent.type(screen.getByLabelText("API Key"), "test-key");
  await userEvent.click(screen.getByRole("button", { name: "获取模型列表" }));
  await screen.findByRole("combobox", { name: "选择模型" });
  await userEvent.click(screen.getByRole("button", { name: "添加模型" }));
  await screen.findByRole("alert");
  await userEvent.click(screen.getByRole("button", { name: "添加模型" }));
  await waitFor(() => expect(done).toHaveBeenCalledOnce());
  expect(keys).toBe(1);
});

test("a service without model listing still allows a model name and optional limits", async () => {
  let saved: Record<string, unknown> | undefined;
  server.use(
    http.post('/api/v1/model-endpoints/discover', () => HttpResponse.json({ code: 'model_discovery_unsupported' }, { status: 422 })),
    http.post('/api/v1/secrets', () => HttpResponse.json({ id: 'key' })),
    http.post('/api/v1/model-endpoints', async ({ request }) => { saved = await request.json() as Record<string, unknown>; return HttpResponse.json({ id: 'ok' }); }),
  );
  const done = setup();
  await userEvent.type(screen.getByLabelText('服务地址'), 'https://models.example.com/v1');
  await userEvent.type(screen.getByLabelText('API Key'), 'test-key');
  await userEvent.click(screen.getByRole('button', { name: '获取模型列表' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('服务不支持读取模型列表');
  await userEvent.click(screen.getByRole('button', { name: '直接填写模型名' }));
  await userEvent.type(screen.getByLabelText('模型'), 'custom-model');
  await userEvent.click(screen.getByRole('button', { name: /可选设置/ }));
  await userEvent.clear(screen.getByLabelText('上下文运行限额'));
  await userEvent.type(screen.getByLabelText('上下文运行限额'), '65536');
  await userEvent.click(screen.getByRole('button', { name: '添加模型' }));
  await waitFor(() => expect(done).toHaveBeenCalledOnce());
  expect(saved).toMatchObject({ model: 'custom-model', context_window: 65536 });
});
