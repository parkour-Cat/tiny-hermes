import { expect, test } from "vitest";
import { readUtf8 } from "./readUtf8";

test("UTF-8 with Chinese and a BOM is readable while invalid bytes are refused", async () => {
  expect(await readUtf8(new File([new Uint8Array([239,187,191]), "中文文档"], "SKILL.md"))).toBe("中文文档");
  await expect(readUtf8(new File([new Uint8Array([0xff,0xfe,0x00])], "SKILL.md"))).rejects.toThrow();
});
