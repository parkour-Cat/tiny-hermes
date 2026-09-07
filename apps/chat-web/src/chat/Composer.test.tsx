import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { Composer } from "./Composer";
import { LocaleProvider } from "../i18n/locale";

function renderComposer(
  props: Partial<Parameters<typeof Composer>[0]> = {},
): ReturnType<typeof render> {
  return render(
    <LocaleProvider>
      <Composer
        disabled={false}
        sending={false}
        live={false}
        canExport
        onSend={() => undefined}
        onStop={() => undefined}
        onExport={() => undefined}
        {...props}
      />
    </LocaleProvider>,
  );
}

test("Enter confirms an IME candidate without sending, then ordinary Enter sends", async () => {
  const sent: unknown[] = [];
  renderComposer({ onSend: (value) => { sent.push(value); } });
  const input = screen.getByLabelText("写给智能体");
  fireEvent.change(input, { target: { value: "中文输入" } });
  await act(async () => { fireEvent.keyDown(input, { key: "Enter", isComposing: true }); });
  expect(sent).toEqual([]);
  expect(input).toHaveValue("中文输入");
  fireEvent.keyDown(input, { key: "Enter", isComposing: false });
  await waitFor(() => expect(sent).toHaveLength(1));
});

afterEach(() => {
  vi.restoreAllMocks();
  window.sessionStorage.clear();
  delete (window as Window & { webkitSpeechRecognition?: unknown }).webkitSpeechRecognition;
});

test("the plus menu holds attach, paste, and export", async () => {
  const exported: string[] = [];
  renderComposer({ onExport: () => exported.push("ok") });
  expect(screen.queryByRole("menuitem", { name: "附件" })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "更多" }));
  expect(screen.getByRole("menuitem", { name: "附件" })).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: "从剪贴板粘贴" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("menuitem", { name: "导出对话" }));
  expect(exported).toEqual(["ok"]);
});

test("refresh restores a text draft only in its own conversation", async () => {
  const first = renderComposer({ draftKey: "user-a:agent-a:session-a" });
  await userEvent.type(screen.getByLabelText("写给智能体"), "刷新后继续编辑");
  first.unmount();
  const other = renderComposer({ draftKey: "user-a:agent-a:session-b" });
  expect(screen.getByLabelText("写给智能体")).toHaveValue("");
  other.unmount();
  renderComposer({ draftKey: "user-a:agent-a:session-a" });
  expect(screen.getByLabelText("写给智能体")).toHaveValue("刷新后继续编辑");
});

test("refresh explains missing attachments and requires reselection or removal before sending", async () => {
  const first = renderComposer({ draftKey: "attachment-draft" });
  await userEvent.type(screen.getByLabelText("写给智能体"), "带上这份说明");
  fireEvent.drop(document.querySelector(".composer")!, {
    dataTransfer: { files: [new File(["notes"], "notes.txt", { type: "text/plain" })] },
  });
  first.unmount();
  renderComposer({ draftKey: "attachment-draft" });
  expect(screen.getByText(/附件需重新选择：notes.txt/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "发送", exact: true })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "移除未恢复附件" }));
  expect(screen.getByRole("button", { name: "发送", exact: true })).toBeEnabled();
});

test("blocked browser storage reports the loss of refresh recovery without blocking typing", async () => {
  renderComposer({ draftKey: "blocked-draft" });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Quota exceeded"); });
  await userEvent.type(screen.getByLabelText("写给智能体"), "仍然可以输入");
  expect(screen.getByLabelText("写给智能体")).toHaveValue("仍然可以输入");
  expect(screen.getByText("当前无法保存输入，刷新页面会丢失这段文字。请先复制保留。")).toBeInTheDocument();
});

test("the composer menu supports keyboard entry, arrows and Escape focus return", async () => {
  renderComposer();
  const user = userEvent.setup();
  await user.tab();
  expect(screen.getByLabelText("写给智能体")).toHaveFocus();
  await user.tab();
  const trigger = screen.getByRole("button", { name: "更多" });
  expect(trigger).toHaveFocus();
  await user.keyboard("{Enter}");
  expect(screen.getByRole("menuitem", { name: "附件" })).toHaveFocus();
  await user.keyboard("{ArrowDown}");
  expect(screen.getByRole("menuitem", { name: "从剪贴板粘贴" })).toHaveFocus();
  await user.keyboard("{End}");
  expect(screen.getByRole("menuitem", { name: "导出对话" })).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("menu")).toBeNull();
  expect(trigger).toHaveFocus();
});

test("export stays off when the thread is empty", async () => {
  renderComposer({ canExport: false });
  await userEvent.click(screen.getByRole("button", { name: "更多" }));
  expect(screen.getByRole("menuitem", { name: "导出对话" })).toBeDisabled();
});

test("dropping or pasting a file stages it on the composer", async () => {
  renderComposer();
  const note = new File(["hello"], "note.txt", { type: "text/plain" });
  const form = document.querySelector(".composer");
  expect(form).not.toBeNull();
  fireEvent.drop(form as Element, { dataTransfer: { files: [note] } });
  expect(screen.getByText("note.txt")).toBeInTheDocument();

  const extra = new File(["more"], "extra.md", { type: "text/markdown" });
  fireEvent.paste(screen.getByLabelText("写给智能体"), { clipboardData: { files: [extra] } });
  expect(screen.getByText("extra.md")).toBeInTheDocument();
});

test("an unreadable attachment reports the error and retains the draft for correction", async () => {
  const sent: string[] = [];
  renderComposer({ onSend: (text) => { sent.push(text); } });
  const file = new File(["notes"], "unreadable.txt", { type: "text/plain" });
  Object.defineProperty(file, "text", { value: async () => { throw new Error("File unreadable"); } });
  fireEvent.drop(document.querySelector(".composer")!, { dataTransfer: { files: [file] } });
  const input = screen.getByLabelText("写给智能体");
  await userEvent.type(input, "保留附件和说明");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));
  expect(await screen.findByText("附件读取失败，请重新选择文件后重试。")).toBeInTheDocument();
  expect(input).toHaveValue("保留附件和说明");
  expect(screen.getByText("unreadable.txt")).toBeInTheDocument();
  expect(sent).toEqual([]);
});

test("voice input appears only when the browser can dictate", async () => {
  const first = renderComposer();
  expect(screen.queryByRole("button", { name: "语音输入" })).toBeNull();
  first.unmount();

  class FakeRecognition {
    continuous = false;
    interimResults = false;
    lang = "";
    onresult = null;
    onerror = null;
    onend = null;
    start(): void {}
    stop(): void {}
  }
  (window as Window & { webkitSpeechRecognition?: unknown }).webkitSpeechRecognition =
    FakeRecognition;
  renderComposer();
  expect(screen.getByRole("button", { name: "语音输入" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "语音输入" }));
  expect(screen.getByRole("button", { name: "正在听" })).toBeInTheDocument();
});
