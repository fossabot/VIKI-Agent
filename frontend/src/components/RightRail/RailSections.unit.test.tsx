/**
 * UpNextSection and RailFooter: the Up next and footer rail groups. These tests only check
 * that whatever is passed in lands in the right place.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { RailFooter, UpNextSection } from "./RailSections";

afterEach(cleanup);

test("UpNextSection always renders routines inside a rail-up-next wrapper", () => {
  const { container } = render(<UpNextSection routines={<div data-testid="routines" />} />);
  const wrapper = container.querySelector(".rail-up-next");
  expect(wrapper).not.toBeNull();
  expect(wrapper?.children).toHaveLength(1);
  expect(wrapper?.firstElementChild?.getAttribute("data-testid")).toBe("routines");
});

test("RailFooter renders tiles, the uncommitted count and skills in order", () => {
  const { container } = render(
    <RailFooter
      tiles={<div data-testid="tiles" />}
      uncommitted={3}
      skills={<div data-testid="skills" />}
    />,
  );
  const marks = [...container.children].map((el) => el.getAttribute("data-testid") ?? el.className);
  expect(marks).toEqual(["tiles", "rail-uncommitted", "skills"]);
  const note = container.querySelector(".rail-uncommitted");
  expect(note?.textContent).toBe("3 uncommitted");
  expect(note?.getAttribute("title")).toBe("Vault changes not yet committed to git");
});

test("RailFooter hides the uncommitted paragraph when the count is 0 or absent", () => {
  const { container, rerender } = render(
    <RailFooter tiles={<div />} uncommitted={0} skills={<div />} />,
  );
  expect(container.querySelector(".rail-uncommitted")).toBeNull();

  rerender(<RailFooter tiles={<div />} skills={<div />} />);
  expect(container.querySelector(".rail-uncommitted")).toBeNull();
});
