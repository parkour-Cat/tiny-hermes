import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { server } from "./server";

// Testing Library only auto-cleans when vitest runs with globals, which this
// project does not. Without this, a second test in a file queries the first
// test's tree as well as its own.
afterEach(cleanup);

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const browserGetComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element: Element): CSSStyleDeclaration =>
  browserGetComputedStyle(element);

/** Media queries the tests are allowed to answer for. */
export const mediaMatches = new Map<string, boolean>();

Object.defineProperty(window, "matchMedia", {
  writable: true,
  // The phase-1 stub answered `false` to every query, which made a dark-mode
  // assertion vacuous. Tests now set `mediaMatches` for the query they care
  // about; everything else still answers `false`.
  value: (query: string) => ({
    matches: mediaMatches.get(query) ?? false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});

afterEach(() => {
  mediaMatches.clear();
  window.localStorage.removeItem("tiny-hermes-chat-default-agent");
  window.localStorage.removeItem("tiny-hermes-chat-theme");
  window.localStorage.removeItem("tiny-hermes-chat-locale");
});

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

globalThis.ResizeObserver = ResizeObserverStub;
