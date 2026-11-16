import { expect, test } from "vitest";
import type { GitChip } from "../protocol";
import { observeGit } from "./git";

const clean: GitChip = {
  state: "known",
  ref_state: "branch",
  branch: "main",
  head: "abc1234",
  committed_at: "",
  dirty: 0,
};

test("a loading or errored status reports git state as unknown while keeping the known head", () => {
  for (const status of ["loading", "error"] as const) {
    const value = observeGit(clean, status);
    expect(value.head).toBe(clean.head);
    expect(value.state).toBe("unknown");
    expect(value.dirty).toBeNull();
  }
  expect(observeGit({ ...clean, dirty: 2 }, "ready").dirty).toBe(2);
  expect(observeGit({ ...clean, state: "not_repository", dirty: null }, "ready").state).toBe(
    "not_repository",
  );
});

test("missing and malformed wire facts fail closed without labelling a detached HEAD", () => {
  for (const chip of [
    undefined,
    {},
    { ...clean, state: undefined },
    { ...clean, ref_state: undefined },
    { ...clean, branch: "" },
    { ...clean, ref_state: "detached", head: "" },
    { ...clean, dirty: -1 },
    { ...clean, dirty: 0.5 },
    { ...clean, dirty: null },
  ]) {
    expect(observeGit(chip as GitChip | undefined, "ready").state).toBe("unknown");
  }
  expect(observeGit({ ...clean, branch: "", ref_state: "unknown" }, "ready").ref_state).toBe(
    "unknown",
  );
  expect(observeGit({ ...clean, branch: "", ref_state: "detached" }, "ready").ref_state).toBe(
    "detached",
  );
});
