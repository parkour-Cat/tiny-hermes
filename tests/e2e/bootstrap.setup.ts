import { expect, test as setup } from "@playwright/test";

import { ADMIN, BOOTSTRAP_TOKEN, CONSOLE_STATE } from "./session";

/**
 * Opens the platform once, and signs in once.
 *
 * Bootstrap creates the first administrator and then closes; two specs that
 * both called it could not both pass. It happens here instead, and every spec
 * that needs a signed-in browser starts from the state this saves.
 */
setup("bootstrap the platform and sign in", async ({ page, request }) => {
  const bootstrap = await request.post("/api/v1/bootstrap", {
    headers: { "X-Bootstrap-Token": BOOTSTRAP_TOKEN },
    data: {
      subject: ADMIN.subject,
      display_name: ADMIN.displayName,
      password: ADMIN.password,
    },
  });
  // `201` on a fresh stack, which is what CI always runs against. A local rerun
  // reaches an already-opened platform, and the only refusal that may mean is
  // `bootstrap_closed`: a wrong token still answers `403` and fails here.
  if (bootstrap.status() !== 201) {
    expect(bootstrap.status()).toBe(409);
    expect((await bootstrap.json()).code).toBe("bootstrap_closed");
  }

  await page.goto("/login");
  await page.getByLabel("邮箱").fill(ADMIN.subject);
  await page.getByLabel("密码").fill(ADMIN.password);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).toHaveURL(/\/workspaces$/);

  await page.context().storageState({ path: CONSOLE_STATE });
});
