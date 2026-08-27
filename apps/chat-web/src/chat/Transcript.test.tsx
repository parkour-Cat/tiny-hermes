import { render, screen } from "@testing-library/react";
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
        artifacts={[]}
        canRetry={canRetry}
        onDownload={() => undefined}
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
        artifacts={[]}
        canRetry={false}
        onDownload={() => undefined}
        onRetry={() => undefined}
      />
    </LocaleProvider>,
  );
  expect(screen.getByText("Summarize yesterday")).toBeTruthy();
  expect(screen.getAllByText("已撤回")).toHaveLength(1);
});
