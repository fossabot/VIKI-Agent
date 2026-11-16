import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { LineDiff } from "./LineDiff";

afterEach(cleanup);

test("marks each row and keeps its kind class", () => {
  const { container } = render(<LineDiff before={"a\nb"} after={"a\nc"} />);
  const rows = [...container.querySelectorAll(".line-diff > div")];
  expect(rows.map((r) => [r.className, r.textContent])).toEqual([
    ["line-diff-same", "  a"],
    ["line-diff-del", "- b"],
    ["line-diff-add", "+ c"],
  ]);
});

test("markup in a line stays text", () => {
  const { container } = render(<LineDiff before="x" after="<b>x</b>" />);
  expect(container.querySelector("b")).toBeNull();
  expect(container.textContent).toContain("+ <b>x</b>");
});
