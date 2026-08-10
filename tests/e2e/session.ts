import path from "node:path";

/**
 * The one account a stack is bootstrapped with, and where its cookies are kept.
 *
 * Shared from a plain module rather than from `bootstrap.setup.ts`: importing a
 * file that registers a test would register it a second time in whichever
 * project imported it.
 */
export const ADMIN = {
  subject: "admin@example.com",
  displayName: "Admin",
  password: "long-pass-123",
};

export const BOOTSTRAP_TOKEN =
  process.env.TINY_HERMES_E2E_BOOTSTRAP_TOKEN ?? "local-bootstrap-token-with-32-characters";

/** Where the setup project leaves the signed-in browser state. */
export const CONSOLE_STATE = path.join(__dirname, ".auth", "console.json");
