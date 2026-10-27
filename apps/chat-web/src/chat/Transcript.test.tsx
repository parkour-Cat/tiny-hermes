import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { Transcript } from "./Transcript";
import { LocaleProvider } from "../i18n/locale";

const turns = [
  { role: "user", parts: [{ type: "text", text: "Summarize yesterday" }] },
  { role: "assistant", parts: [{ type: "text", text: "Here is the summary." }] },
];

function renderTranscript(canRetry = false, onRetry = () => undefined): void {
  render(
    <LocaleProvider>
      <Transcript
        turns={turns}
        optimistic={null}
        live={false}
        savedFiles={[]}
        savingFile={null}
        canRetry={canRetry}
        onSaveFile={() => undefined}
        onRetry={onRetry}
      />
    </LocaleProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

test("each finished line can be copied", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  renderTranscript();
  const copies = screen.getAllByRole("button", { name: "复制" });
  expect(copies).toHaveLength(2);
  await userEvent.click(copies[1] as HTMLElement);
  expect(writeText).toHaveBeenCalledWith("Here is the summary.");
  expect(screen.getByText("已复制")).toBeInTheDocument();
});

test("retry stays on the last assistant turn, not in chrome", async () => {
  const retries: string[] = [];
  renderTranscript(true, () => {
    retries.push("retry");
  });
  expect(screen.queryByRole("button", { name: "重试" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(screen.getByText(/再跑一次/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "确认重试" }));
  expect(retries).toEqual(["retry"]);
});

test("a turn without retry does not offer it", () => {
  renderTranscript(false);
  expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
});

test("a withdrawn turn is visibly marked", () => {
  // 发 /undo 的人就是读这份转写的人。那一轮仍然留在这里，因为它确实被说过；
  // 这个标记是它和模型仍在读的一轮之间唯一的区别。
  render(
    <LocaleProvider>
      <Transcript
        turns={[
          {
            role: "user",
            parts: [{ type: "text", text: "Summarize yesterday" }],
            withdrawn_at: "2026-08-26T10:43:00Z",
          },
          { role: "assistant", parts: [{ type: "text", text: "Here is the summary." }] },
        ]}
        optimistic={null}
        live={false}
        savedFiles={[]}
        savingFile={null}
        canRetry={false}
        onSaveFile={() => undefined}
        onRetry={() => undefined}
      />
    </LocaleProvider>,
  );
  expect(screen.getByText("Summarize yesterday")).toBeTruthy();
  expect(screen.getAllByText("已撤回")).toHaveLength(1);
});

/* Shapes copied from a real session's rows (session_messages, 2026-10-26):
   a call sits in an assistant message, its result in the next `tool` one. */
const call = (id: string, name: string, args: Record<string, unknown>) => ({
  role: "assistant",
  parts: [{ type: "reasoning", text: "…" }, { type: "tool_call", call_id: id, name, arguments: args }],
});
const result = (id: string, output: string, failed = false) => ({
  role: "tool",
  parts: [{ type: "tool_result", call_id: id, output, exit_code: failed ? 2 : 0, failed }],
});

function renderTurns(
  turns: Parameters<typeof Transcript>[0]["turns"],
  { live = false, savedFiles = [] as { path: string; size_bytes: number }[], onSaveFile = (_: string) => undefined } = {},
): void {
  render(
    <LocaleProvider>
      <Transcript
        turns={turns}
        optimistic={null}
        live={live}
        savedFiles={savedFiles}
        savingFile={null}
        canRetry={false}
        onSaveFile={onSaveFile}
        onRetry={() => undefined}
      />
    </LocaleProvider>,
  );
}

test("a reply's tool calls fold into one line that opens to readable steps", () => {
  renderTurns([
    { role: "user", parts: [{ type: "text", text: "做个 PPT" }] },
    call("c1", "file.list", { path: "notes" }),
    result("c1", '{"error":"No such file or directory"}\n', true),
    call("c2", "shell.exec", { command: "cd out && python3 make_ppt.py" }),
    result("c2", "saved\n"),
    { role: "assistant", parts: [{ type: "text", text: "PPT 做好了" }] },
  ]);
  const work = screen.getByText(/做了 2 步/).closest("details")!;
  expect(work).not.toHaveAttribute("open");
  expect(screen.getByText(/1 步失败/)).toBeInTheDocument();
  expect(within(work).getByText("看目录")).toBeInTheDocument();
  expect(within(work).getByText("notes")).toBeInTheDocument();
  expect(within(work).getByText("cd out && python3 make_ppt.py")).toBeInTheDocument();
  // One reply, not one card per call and another per result.
  expect(screen.getAllByRole("button", { name: "复制" })).toHaveLength(2);
});

test("the reply still working stays open and says which step it is on", () => {
  renderTurns(
    [
      { role: "user", parts: [{ type: "text", text: "做个 PPT" }] },
      call("c1", "file.read", { path: "inbox/a.xlsx" }),
      result("c1", "ok"),
      call("c2", "shell.exec", { command: "python3 chart.py" }),
    ],
    { live: true },
  );
  const work = screen.getByText(/正在做第 2 步/).closest("details")!;
  expect(work).toHaveAttribute("open");
});

test("a file sent back is a card that downloads from the session's files", async () => {
  const saved: string[] = [];
  renderTurns(
    [
      { role: "user", parts: [{ type: "text", text: "做个 PPT" }] },
      call("c1", "file.send", { path: "out/Gemini 4 Argon 模型介绍.pptx" }),
      result(
        "c1",
        "This conversation is not in Feishu, so nothing was sent. Gemini 4 Argon 模型介绍.pptx is saved in the workspace at out/Gemini 4 Argon 模型介绍.pptx; tell the person they can download it from this Run's files.",
      ),
      { role: "assistant", parts: [{ type: "text", text: "做好了" }] },
    ],
    {
      savedFiles: [{ path: "out/Gemini 4 Argon 模型介绍.pptx", size_bytes: 152141 }],
      onSaveFile: (path) => void saved.push(path),
    },
  );
  const card = screen.getByRole("group", { name: "Gemini 4 Argon 模型介绍.pptx" });
  expect(within(card).getByText("149 KB")).toBeInTheDocument();
  await userEvent.click(within(card).getByRole("button", { name: "下载" }));
  expect(saved).toEqual(["out/Gemini 4 Argon 模型介绍.pptx"]);
});

test("a sent file no longer among the session's files says so instead of offering a download", () => {
  renderTurns([
    { role: "user", parts: [{ type: "text", text: "做个 PPT" }] },
    call("c1", "file.send", { path: "out/a.pptx" }),
    result("c1", "saved"),
  ]);
  const card = screen.getByRole("group", { name: "a.pptx" });
  expect(within(card).getByText("已不在会话文件里")).toBeInTheDocument();
  expect(within(card).queryByRole("button")).toBeNull();
});
