import "@testing-library/jest-dom/vitest";

import { afterAll, afterEach, beforeAll } from "vitest";

import { server } from "./server";

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
