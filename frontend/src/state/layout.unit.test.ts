import { expect, test } from "vitest";
import { clampResize } from "./layout";

test.each([
  [239.4, 200, 500, 239],
  [239.5, 200, 500, 240],
  [-10, 200, 500, 200],
  [900, 200, 500, 500],
  [4, 0, 0, 0],
  [7, 10, 10, 10],
])("clamps %s to [%s, %s]", (value, min, max, expected) => {
  expect(clampResize(value, min, max)).toBe(expected);
});
