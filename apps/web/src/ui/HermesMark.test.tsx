import { render } from "@testing-library/react";
import { expect, test } from "vitest";

import { HermesMark } from "./HermesMark";

test("the mark is the circular artwork without the word", () => {
  const { container } = render(<HermesMark />);
  const img = container.querySelector("img.th-hermes-mark");
  expect(img).not.toBeNull();
  expect(img).toHaveAttribute("src", "/tiny-hermes-icon.png");
  expect(img).toHaveAttribute("alt", "");
});

test("the hero and empty states use the named lockup", () => {
  const hero = render(<HermesMark variant="hero" />);
  const heroImg = hero.container.querySelector("img.th-hermes-hero");
  expect(heroImg).toHaveAttribute("src", "/tiny-hermes-lockup.png");
  expect(hero.getByAltText("TINY-HERMES")).toBeInTheDocument();

  const empty = render(<HermesMark variant="empty" size={128} />).container;
  const emptyImg = empty.querySelector("img.th-hermes-empty");
  expect(emptyImg).toHaveAttribute("src", "/tiny-hermes-lockup.png");
  expect(emptyImg).toHaveAttribute("alt", "");
});
