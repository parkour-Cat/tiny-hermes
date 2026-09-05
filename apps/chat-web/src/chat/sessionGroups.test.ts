import { expect, test } from "vitest";

import { filterSessions, groupOf, groupSessions } from "./sessionGroups";

const NOW = new Date("2026-08-17T09:00:00");

function at(iso: string) {
  return { id: iso, title: iso, createdAt: iso };
}

test("bands are calendar days, not elapsed hours", () => {
  // Ten minutes old, but yesterday by the clock on the wall.
  expect(groupOf("2026-08-16T23:50:00", new Date("2026-08-17T00:10:00"))).toBe("yesterday");
  expect(groupOf("2026-08-17T00:05:00", NOW)).toBe("today");
});

test("the week ends where earlier begins", () => {
  expect(groupOf("2026-08-11T09:00:00", NOW)).toBe("week");
  expect(groupOf("2026-08-10T23:59:00", NOW)).toBe("earlier");
});

test("an unreadable timestamp is filed under earlier rather than thrown", () => {
  expect(groupOf("not a date", NOW)).toBe("earlier");
});

test("empty bands do not become empty headings", () => {
  const groups = groupSessions([at("2026-08-17T08:00:00"), at("2026-08-01T08:00:00")], NOW);
  expect(groups.map((group) => group.key)).toEqual(["today", "earlier"]);
});

test("order inside a band is the order it was given", () => {
  const first = at("2026-08-17T08:00:00");
  const second = at("2026-08-17T07:00:00");
  const [today] = groupSessions([first, second], NOW);
  expect(today?.sessions).toEqual([first, second]);
});

test("every word has to appear, in any order", () => {
  const rows = [
    { title: "the report, weekly" },
    { title: "weekly standup" },
    { title: "写一份周报" },
  ];
  expect(filterSessions(rows, "weekly report")).toEqual([{ title: "the report, weekly" }]);
  expect(filterSessions(rows, "周报")).toEqual([{ title: "写一份周报" }]);
  expect(filterSessions(rows, "  ")).toEqual(rows);
});
