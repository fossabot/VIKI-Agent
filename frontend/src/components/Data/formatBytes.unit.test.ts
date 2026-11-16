import { expect, test } from "vitest";
import { formatBytes } from "./formatBytes";

test("bytes under 1024 show the exact count with no decimal", () => {
  expect(formatBytes(0)).toBe("0 B");
  expect(formatBytes(1023)).toBe("1023 B");
});

test("each unit boundary rounds to one decimal", () => {
  expect(formatBytes(1024)).toBe("1.0 KB");
  expect(formatBytes(1536)).toBe("1.5 KB");
  expect(formatBytes(1024 * 1024)).toBe("1.0 MB");
  expect(formatBytes(1024 * 1024 * 1024)).toBe("1.0 GB");
});

test("stops climbing units at TB", () => {
  expect(formatBytes(1024 ** 5)).toBe("1024.0 TB");
});
