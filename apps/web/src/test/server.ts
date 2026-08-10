import { setupServer } from "msw/node";

/**
 * The request interceptor every test declares its handlers against.
 *
 * It ships with no default handlers on purpose. Combined with
 * `onUnhandledRequest: "error"` in the setup file, a page that fires a request
 * no test declared fails loudly instead of quietly receiving `undefined`.
 */
export const server = setupServer();
