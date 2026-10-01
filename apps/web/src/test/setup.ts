import "@testing-library/jest-dom/vitest";

import { cleanup, configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { server } from "./server";

// Testing Library only auto-cleans when vitest runs with globals, which this
// project does not. Without this, a second test in a file queries the first
// test's tree as well as its own.
afterEach(cleanup);

// How long `findBy*` and `waitFor` wait before failing; a passing check returns
// as soon as it holds. The default 1s was too tight for a full page here: the
// Agent page's first select took 0.6s on an idle machine, and the whole suite
// under CPU load failed on it in 2 of 3 runs (2026-10-01).
configure({ asyncUtilTimeout: 5_000 });

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

afterEach(() => mediaMatches.clear());

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

globalThis.ResizeObserver = ResizeObserverStub;
