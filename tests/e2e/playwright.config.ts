import { readdirSync } from "node:fs";

import { defineConfig } from "@playwright/test";

import { CONSOLE_STATE } from "./session";

/**
 * Every spec is claimed by exactly one project, checked here rather than
 * hoped for.
 *
 * `projects` below is an allowlist of `testMatch` patterns, so a spec file
 * nobody listed is simply **not run** — and `Running 17 tests` looks exactly
 * like `Running 18 tests` in a log unless somebody counts. `channels.spec.ts`
 * shipped once that way, green, having executed nothing.
 *
 * Throwing at config load turns that into a failure at the moment it is
 * introduced, which is the only moment it is cheap.
 */
function assertEverySpecIsClaimed(projects: { testMatch: RegExp }[]): void {
  // Read from `projects` itself rather than a second list beside it: a
  // duplicate of these patterns would drift, and the drift would look
  // exactly like the bug this guards against.
  const unclaimed = readdirSync(__dirname)
    .filter((entry) => entry.endsWith(".spec.ts"))
    .filter((entry) => !projects.some((project) => project.testMatch.test(entry)));
  if (unclaimed.length > 0) {
    throw new Error(
      `No Playwright project runs: ${unclaimed.join(", ")}. ` +
        "Add a project in playwright.config.ts, or the walk is dead code.",
    );
  }
}


const PROJECTS = [
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
      testMatch: /(console|stream-contract|audit)\.spec\.ts$/,
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
    // §13's walk. Needs no egress and no sandbox — the children here run
    // platform tools only — but it does need a **Scheduler**, because nothing
    // in the request path settles a `child_runs` wait. On a stack without one
    // the parent waits until its deadline, which is the deployment being
    // honest rather than this walk failing.
    {
      name: "children",
      testMatch: /children\.spec\.ts$/,
      dependencies: ["setup"],
      use: { storageState: CONSOLE_STATE },
    },
    // §14.1's walk. Needs no egress and no sandbox — memory is answered by the
    // platform itself — so it runs on any stack the console runs on.
    {
      name: "memory",
      testMatch: /memory\.spec\.ts$/,
      dependencies: ["setup"],
      use: { storageState: CONSOLE_STATE },
    },
    // §20.1's Channels walk. Needs no egress, no sandbox and no Scheduler:
    // it creates a binding and closes it again, and never delivers anything.
    //
    // Listed here because this file is an allowlist. A spec that matches no
    // project is silently not run — `Running 17 tests` looks exactly like
    // `Running 18 tests` unless somebody counts, and this walk shipped once
    // without running at all.
    {
      name: "channels",
      testMatch: /channels\.spec\.ts$/,
      dependencies: ["setup"],
      use: { storageState: CONSOLE_STATE },
    },
    // Design §7's walk. `page` starts signed in as the console admin — this
    // is what registers the issuer and publishes the Agent, since the
    // builder has no UI for either yet — but the conversation itself runs
    // in a fresh context the test opens for itself, with no console cookie
    // in it. Needs the chat-web service on TINY_HERMES_E2E_CHAT_URL
    // (default http://127.0.0.1:3001, deploy/compose/compose.yaml's own
    // port for it) in addition to the console this stack already needs.
    {
      name: "end-user",
      testMatch: /end-user\.spec\.ts$/,
      dependencies: ["setup"],
      use: { storageState: CONSOLE_STATE },
    },
];

assertEverySpecIsClaimed(
  PROJECTS.filter(
    (project): project is typeof project & { testMatch: RegExp } =>
      project.testMatch instanceof RegExp && project.name !== "setup",
  ),
);

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
  projects: PROJECTS,
});
