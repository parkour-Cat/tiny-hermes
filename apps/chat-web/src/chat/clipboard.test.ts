import { afterEach, expect, test, vi } from "vitest";

import { copyText, readClipboardPayload } from "./clipboard";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("copyText writes the string to the clipboard", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  expect(await copyText("hello")).toBe(true);
  expect(writeText).toHaveBeenCalledWith("hello");
});

test("readClipboardPayload takes text and non-text items", async () => {
  const read = vi.fn().mockResolvedValue([
    {
      types: ["text/plain", "image/png"],
      getType: async (type: string) =>
        type === "text/plain"
          ? new Blob(["pasted note"], { type: "text/plain" })
          : new Blob([new Uint8Array([1, 2])], { type: "image/png" }),
    },
  ]);
  vi.stubGlobal("navigator", { clipboard: { read } });
  const payload = await readClipboardPayload();
  expect(payload).toMatchObject({ ok: true, text: "pasted note" });
  if (payload.ok) {
    expect(payload.files.map((item) => item.name)).toEqual(["clipboard-1.png"]);
  }
});

test("an empty clipboard is empty, a blocked one is denied", async () => {
  vi.stubGlobal("navigator", {
    clipboard: {
      read: vi.fn().mockResolvedValue([]),
      readText: vi.fn().mockResolvedValue("   "),
    },
  });
  expect(await readClipboardPayload()).toEqual({ ok: false, reason: "empty" });

  vi.stubGlobal("navigator", {
    clipboard: { read: vi.fn().mockRejectedValue(new Error("denied")) },
  });
  expect(await readClipboardPayload()).toEqual({ ok: false, reason: "denied" });
});
