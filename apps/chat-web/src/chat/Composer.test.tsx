import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test } from "vitest";

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

afterEach(() => {
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
