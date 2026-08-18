import { defineConfig } from "@playwright/test";

import { CONSOLE_STATE } from "./session";

export default defineConfig({
  testDir: ".",
  retries: 0,
  workers: 1,
  // A Run is executed by a real Worker across real slices, so the console walk
  // waits on work rather than on rendering. The default thirty seconds is a
  // budget for a page, not for a platform.
  timeout: 180_000,
  expect: { timeout: 60_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    // The demo stack's address by default. An isolated stack brought up beside
    // it — the procedure in `docs/development.md` for a platform that was
    // bootstrapped with some other account — publishes different host ports,
    // and this is how the walk is pointed at it.
    baseURL: process.env.TINY_HERMES_E2E_BASE_URL ?? "http://127.0.0.1:3000",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "setup", testMatch: /.*\.setup\.ts$/ },
    // Signing in is what this one is about, so it starts signed out and does it
    // through the form; it only needs the account to exist.
    {
      name: "foundation",
      testMatch: /foundation\.spec\.ts$/,
      dependencies: ["setup"],
    },
    {
      name: "console",
      testMatch: /(console|stream-contract)\.spec\.ts$/,
      dependencies: ["setup"],
      use: { storageState: CONSOLE_STATE },
    },
    // Its own project rather than another file in `console`: this walk runs
    // two Runs end to end and needs the whole catalog, and keeping it separate
    // means a failure says which of the two areas broke.
    {
      name: "skills",
      testMatch: /skills\.spec\.ts$/,
      dependencies: ["setup"],
      use: { storageState: CONSOLE_STATE },
    },
    // Its own project for the same reason, and with one extra requirement it
    // states rather than assumes: this walk calls out through the egress
    // proxy, so it needs a stack where `EGRESS_PROXY_URL` is set and
    // `OUTBOUND_ALLOWED_CIDRS` covers the Compose bridge. Without those every
    // outbound call refuses — which is M2C-1 working, not this walk failing.
    {
      name: "tools",
      testMatch: /tools\.spec\.ts$/,
      dependencies: ["setup"],
      use: { storageState: CONSOLE_STATE },
    },
  ],
});
