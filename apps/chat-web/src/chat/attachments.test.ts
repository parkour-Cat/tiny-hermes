import { expect, test } from "vitest";

import { canInlineAttachment, composeWithAttachments } from "./attachments";

test("text attachments are inlined and binaries are skipped", async () => {
  expect(canInlineAttachment({ name: "note.txt", type: "text/plain" })).toBe(true);
  expect(canInlineAttachment({ name: "photo.png", type: "image/png" })).toBe(false);

  const note = {
    name: "note.txt",
    size: 5,
    type: "text/plain",
    file: new File(["hello"], "note.txt", { type: "text/plain" }),
  };
  const photo = {
    name: "photo.png",
    size: 4,
    type: "image/png",
    file: new File([new Uint8Array([1, 2, 3, 4])], "photo.png", { type: "image/png" }),
  };
  const composed = await composeWithAttachments("请看", [note, photo]);
  expect(composed.text).toContain("请看");
  expect(composed.text).toContain("附件 note.txt:");
  expect(composed.text).toContain("hello");
  expect(composed.skipped).toEqual(["photo.png"]);
});
