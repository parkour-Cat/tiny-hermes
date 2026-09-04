import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { expect, test } from "vitest";

import { LEGACY_REDIRECTS } from "./layout/redirects";

const WORKSPACE = "11111111-2222-4333-8444-555555555555";

/** Says where the router ended up. `MemoryRouter` does not touch
 *  `window.location`, so the assertion reads the router itself. */
function Probe() {
  const location = useLocation();
  return <p data-testid="where">{`${location.pathname}${location.hash}`}</p>;
}

function renderAt(path: string): void {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/workspaces/:workspaceId">
          {["inbox", "tooling", "records", "settings"].map((group) => (
            <Route key={group} path={group} element={<Probe />} />
          ))}
          {LEGACY_REDIRECTS.map(([from, to, anchor]) => (
            <Route
              key={from}
              path={from}
              element={<Navigate to={`../${to}#${anchor}`} replace />}
            />
          ))}
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

test.each(LEGACY_REDIRECTS.map(([from, to, anchor]) => [from, to, anchor]))(
  "旧地址 /%s 落在 /%s 上",
  async (old, group, anchor) => {
    // 这些地址可能被存过书签，也可能出现在别处。跳转是长期行为，不是过渡措施。
    renderAt(`/workspaces/${WORKSPACE}/${old}`);
    await waitFor(() =>
      expect(screen.getByTestId("where")).toHaveTextContent(
        `/workspaces/${WORKSPACE}/${group}#${anchor}`,
      ),
    );
  },
);

test("十五个旧地址，一个不少", () => {
  expect(LEGACY_REDIRECTS).toHaveLength(15);
});
