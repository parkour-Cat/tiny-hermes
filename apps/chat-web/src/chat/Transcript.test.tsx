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
