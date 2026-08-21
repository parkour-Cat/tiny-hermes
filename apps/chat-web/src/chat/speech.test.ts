import { afterEach, expect, test } from "vitest";

import { canDictate, startDictation } from "./speech";

type Host = Window & { webkitSpeechRecognition?: unknown; SpeechRecognition?: unknown };

afterEach(() => {
  delete (window as Host).webkitSpeechRecognition;
  delete (window as Host).SpeechRecognition;
});

test("dictation is hidden when the browser has no speech API", () => {
  expect(canDictate()).toBe(false);
});

test("dictation starts and reports a final transcript", () => {
  const langs: string[] = [];
  class FakeRecognition {
    continuous = false;
    interimResults = false;
    lang = "";
    onresult: ((event: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void) | null =
      null;
    onerror: ((event: { error: string }) => void) | null = null;
    onend: (() => void) | null = null;
    start(): void {
      langs.push(this.lang);
      this.onresult?.({
        resultIndex: 0,
        results: [{ isFinal: true, 0: { transcript: "打开报告" } }],
      });
    }
    stop(): void {
      this.onend?.();
    }
  }
  (window as Host).webkitSpeechRecognition = FakeRecognition;
  const texts: { text: string; final: boolean }[] = [];
  const ended: string[] = [];
  const handle = startDictation(
    "zh-CN",
    (text, isFinal) => texts.push({ text, final: isFinal }),
    () => ended.push("end"),
  );
  expect(canDictate()).toBe(true);
  expect(handle).not.toBeNull();
  expect(langs).toEqual(["zh-CN"]);
  handle?.stop();
  expect(texts).toEqual([{ text: "打开报告", final: true }]);
  expect(ended).toEqual(["end"]);
});
