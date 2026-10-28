import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { expect, test } from "vitest";

import { SavedFiles } from "./SavedFiles";
import { useSavedFiles } from "./useSavedFiles";
import { LocaleProvider } from "../i18n/locale";
import { server } from "../test/server";

const SESSION = "11111111-2222-4333-8444-555555555555";

function Panel() {
  return <SavedFiles saved={useSavedFiles(SESSION)} />;
}

function renderPanel(items: { path: string; size_bytes: number }[]) {
  server.use(
    http.get(`/api/v1/end-user/sessions/${SESSION}/files`, () =>
      HttpResponse.json({ revision_id: "revision-1", items }),
    ),
  );
  return render(
    <LocaleProvider>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <Panel />
      </QueryClientProvider>
    </LocaleProvider>,
  );
}

test("a file reads like the card in the reply: its name, where it is, and a size a person can read", async () => {
  renderPanel([{ path: "out/Gemini 4 Argon 模型介绍.pptx", size_bytes: 152_141 }]);
  const row = await screen.findByRole("group", { name: "Gemini 4 Argon 模型介绍.pptx" });
  expect(row).toHaveTextContent("149 KB");
  expect(row).toHaveTextContent("out/");
  expect(row).not.toHaveTextContent("152,141");
  // The download keeps naming the whole path, so two files of one name in
  // different folders stay two different buttons.
  expect(screen.getByRole("button", { name: "下载 out/Gemini 4 Argon 模型介绍.pptx" })).toBeEnabled();
});

test("a conversation with no files shows no panel, rather than a line under every new chat", async () => {
  let asked = false;
  server.use(
    http.get(`/api/v1/end-user/sessions/${SESSION}/files`, () => {
      asked = true;
      return HttpResponse.json({ revision_id: null, items: [] });
    }),
  );
  const { container } = render(
    <LocaleProvider>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <Panel />
      </QueryClientProvider>
    </LocaleProvider>,
  );
  await waitFor(() => expect(asked).toBe(true));
  // Let the answer land before asserting on what it left on screen.
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect(container.querySelector(".saved-files")).toBeNull();
  expect(screen.queryByText("还没有保存的文件")).toBeNull();
});

test("a listing that failed still says so, even with nothing to list", async () => {
  server.use(
    http.get(`/api/v1/end-user/sessions/${SESSION}/files`, () => HttpResponse.json({}, { status: 503 })),
  );
  render(
    <LocaleProvider>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <Panel />
      </QueryClientProvider>
    </LocaleProvider>,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("文件列表读取失败");
  expect(screen.getByRole("button", { name: "刷新文件" })).toBeEnabled();
});
