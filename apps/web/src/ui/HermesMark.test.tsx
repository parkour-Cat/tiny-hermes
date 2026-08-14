import { render } from "@testing-library/react";
import { expect, test } from "vitest";

import { HermesMark } from "./HermesMark";

test("the mark is a circular badge with a headphone valve", () => {
  const { container } = render(<HermesMark />);
  const svg = container.querySelector("svg.th-hermes-mark");
  expect(svg).not.toBeNull();
  expect(container.querySelector(".th-hermes-disc")).not.toBeNull();
  expect(container.querySelector(".th-hermes-figure")).not.toBeNull();
  expect(container.querySelector(".th-hermes-face")).not.toBeNull();
  expect(container.querySelector(".th-hermes-bangs")).not.toBeNull();
  expect(container.querySelectorAll("circle.th-hermes-cup")).toHaveLength(2);
  expect(container.querySelector(".th-hermes-rim")).not.toBeNull();
  expect(container.querySelectorAll("rect.th-hermes-pause")).toHaveLength(2);
});

test("the hero keeps the eye; the tiny mark does not", () => {
  const hero = render(<HermesMark variant="hero" />).container;
  expect(hero.querySelector(".th-hermes-disc")).not.toBeNull();
  expect(hero.querySelector(".th-hermes-fine")).not.toBeNull();

  const mark = render(<HermesMark variant="mark" />).container;
  expect(mark.querySelector(".th-hermes-fine")).toBeNull();
});
