import type { GitChip } from "../protocol";
import type { RestSnapshot } from "./rest";

/** Older/malformed responses and retained data from failed reads cannot certify clean. */
export function observeGit(
  chip: GitChip | undefined,
  status: RestSnapshot["railsStatus"],
): GitChip {
  const branch = typeof chip?.branch === "string" ? chip.branch : "";
  const head = typeof chip?.head === "string" ? chip.head : "";
  const committed_at = typeof chip?.committed_at === "string" ? chip.committed_at : "";
  const ref_state =
    chip?.ref_state === "branch" && branch
      ? "branch"
      : chip?.ref_state === "detached" && head && !branch
        ? "detached"
        : "unknown";
  const dirty =
    typeof chip?.dirty === "number" && Number.isSafeInteger(chip.dirty) && chip.dirty >= 0
      ? chip.dirty
      : null;
  const state =
    status !== "ready" ||
    typeof chip?.branch !== "string" ||
    typeof chip?.head !== "string" ||
    typeof chip?.committed_at !== "string" ||
    !["branch", "detached", "unknown"].includes(chip?.ref_state ?? "") ||
    (chip?.ref_state === "branch" && !branch) ||
    (chip?.ref_state === "unknown" && branch !== "") ||
    (chip?.ref_state === "detached" && (!head || branch !== ""))
      ? "unknown"
      : chip?.state === "known" && dirty !== null
        ? "known"
        : chip?.state === "not_repository" && chip.dirty === null
          ? "not_repository"
          : "unknown";
  return { branch, head, committed_at, ref_state, state, dirty: state === "known" ? dirty : null };
}
