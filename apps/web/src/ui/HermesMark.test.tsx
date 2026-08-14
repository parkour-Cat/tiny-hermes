import { render } from "@testing-library/react";
import { expect, test } from "vitest";

import { HermesMark } from "./HermesMark";

test("the mark is a ringed listener with a pause cup", () => {
  const { container } = render(<HermesMark />);
  expect(container.querySelector("svg.th-hermes-mark")).not.toBeNull();
  expect(container.querySelector(".th-hermes-ring")).not.toBeNull();
  expect(container.querySelector(".th-hermes-ink")).not.toBeNull();
  expect(container.querySelector(".th-hermes-face")).not.toBeNull();
  expect(container.querySelectorAll("circle.th-hermes-cup")).toHaveLength(2);
  expect(container.querySelectorAll("rect.th-hermes-pause")).toHaveLength(2);
  expect(container.querySelector(".th-hermes-banner")).toBeNull();
});

test("the hero lockup names the product; the tiny mark does not", () => {
  const hero = render(<HermesMark variant="hero" />);
  expect(hero.container.querySelector(".th-hermes-banner")).not.toBeNull();
  expect(hero.container.querySelector(".th-hermes-fine")).not.toBeNull();
  expect(hero.getByText("TINY-HERMES")).toBeInTheDocument();

  const mark = render(<HermesMark variant="mark" />).container;
  expect(mark.querySelector(".th-hermes-banner")).toBeNull();
  expect(mark.querySelector(".th-hermes-fine")).toBeNull();
});
