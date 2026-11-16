import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type { ChecksApi } from "../../clients/code/checksApi";
import {
  type CommitJson,
  type IntegrationJson,
  type ReviewApi,
  ReviewApiError,
  type ReviewEntryJson,
  type ReviewJson,
  type ReviewKind,
  type ReviewViewJson,
} from "../../clients/code/reviewApi";
import type { WorktreeJson } from "../../clients/code/rootsApi";
import { noteWorktree, resetRoots } from "../../state/sessionRoots";
import { Review } from "./Review";

const W = "00000000-0000-4000-8000-0000000000a1";
const OTHER = "00000000-0000-4000-8000-0000000000a2";
const HEAD = "1111111111111111111111111111111111111111";
const NEXT = "2222222222222222222222222222222222222222";
const TARGET = "3333333333333333333333333333333333333333";
const D1 = "a".repeat(64);
const D2 = "b".repeat(64);
const COMMIT = "Commit selected files";

afterEach(() => {
  cleanup();
  resetRoots();
  vi.restoreAllMocks();
});

const folder = (over: Partial<WorkspaceJson> = {}, rootId: string | null = "1:2") =>
  ({
    workspace_id: W,
    kind: "code",
    name: "Alpha",
    policy: "ordinary",
    policy_revision: 3,
    root_physical_id: rootId ?? undefined,
    root: "x",
    root_available: true,
    ...over,
  }) as WorkspaceJson;

let counter = 0;
const worktree = (over: Partial<WorktreeJson> = {}): WorktreeJson => {
  counter += 1;
  return {
    worktree_id: `00000000-0000-4000-8000-${String(counter).padStart(12, "0")}`,
    workspace_id: W,
    branch: `viki/b${counter}`,
    base_oid: HEAD,
    path: `C:\\area\\wt${counter}`,
    state: "ready",
    created_at: "2026-10-03T10:00:00Z",
    updated_at: "2026-10-03T10:00:00Z",
    ...over,
  };
};

const text = (path: string, digest = D1): ReviewEntryJson => ({
  path,
  change: "modified",
  kind: "text",
  digest,
});
const other = (path: string, kind: ReviewKind): ReviewEntryJson => ({
  path,
  change: "added",
  kind,
});

const review = (over: Partial<ReviewJson> = {}): ReviewJson => ({
  workspace_id: W,
  worktree_id: null,
  branch: "main",
  head_oid: HEAD,
  in_progress: false,
  entries: [text("src/a.ts", D1), text("src/b.ts", D2), other("logo.png", "binary")],
  truncated: false,
  remote: null,
  remote_state: "none",
  remote_oid: null,
  pull_request_url: null,
  integration: null,
  ...over,
});

const integration = (over: Partial<IntegrationJson> = {}): IntegrationJson => ({
  target_branch: "main",
  target_oid: TARGET,
  merge_base: TARGET,
  fast_forward: true,
  ahead: 1,
  behind: 0,
  commits: [{ oid: NEXT, subject: "Add the thing" }],
  commits_truncated: false,
  paths: [{ path: "src/a.ts", change: "modified", kind: "text" }],
  paths_truncated: false,
  ...over,
});

const committed = (over: Partial<CommitJson> = {}): CommitJson => ({
  outcome: "committed",
  head_oid: NEXT,
  hook_changed: [],
  unexpected: [],
  ...over,
});

const unused = () => Promise.reject(new Error("not stubbed"));
function stub(over: Partial<ReviewApi> = {}): ReviewApi {
  return {
    review: vi.fn().mockResolvedValue(review()),
    view: unused,
    commit: unused,
    integrate: unused,
    push: unused,
    check: unused,
    ...over,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const settle = () => act(async () => {});

async function setup(
  api: ReviewApi,
  props: {
    folder?: WorkspaceJson;
    initialWorktree?: string | null;
    checks?: ChecksApi;
  } = {},
) {
  const onOpenUrl = vi.fn();
  render(
    <Review
      api={api}
      {...(props.checks ? { checks: props.checks } : {})}
      folder={props.folder ?? folder()}
      initialWorktree={props.initialWorktree ?? null}
      onOpenUrl={onOpenUrl}
    />,
  );
  await settle();
  return { onOpenUrl };
}

const click = async (name: string | RegExp) => {
  fireEvent.click(screen.getByRole("button", { name }));
  await settle();
};
const tick = (path: string) =>
  fireEvent.click(screen.getByRole("checkbox", { name: `Include ${path}` }));
const write = (message: string) =>
  fireEvent.change(screen.getByLabelText("Commit message"), { target: { value: message } });
const commitButton = () => screen.getByRole("button", { name: COMMIT }) as HTMLButtonElement;
const form = () => {
  const found = screen.getByLabelText("Commit message").closest("form");
  if (!found) throw new Error("no commit form");
  return within(found);
};
/** The Review's own live region comes first; the panels each hold one after it. */
const ownStatus = () => screen.getAllByRole("status")[0] as HTMLElement;
const reviewCalls = (api: ReviewApi) => (api.review as ReturnType<typeof vi.fn>).mock.calls;

describe("check evidence", () => {
  test("shows the checks only when a checks client is given", async () => {
    const list = vi.fn().mockResolvedValue([]);
    await setup(stub());
    expect(screen.queryByRole("heading", { name: "Checks" })).toBeNull();
    cleanup();
    await setup(stub(), { checks: { list } as unknown as ChecksApi });
    expect(screen.getByRole("heading", { name: "Checks" })).toBeTruthy();
    expect(list).toHaveBeenCalledWith(W, null, expect.anything());
  });

  test("the mount reads once, and Refresh reads again and shows the newer answer", async () => {
    const list = vi
      .fn()
      .mockResolvedValueOnce([{ name: "ok", readable: true, check_id: "c1", last_run: null }])
      .mockResolvedValueOnce([
        {
          name: "ok",
          readable: true,
          check_id: "c1",
          last_run: { outcome: "passed", head_oid: HEAD, dirty: false },
        },
      ]);
    await setup(stub(), { checks: { list } as unknown as ChecksApi });
    expect(list).toHaveBeenCalledTimes(1);
    expect(screen.getByText("ok — not run here yet")).toBeTruthy();
    await click("Refresh");
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.getByText("ok — Passed · matches this commit")).toBeTruthy();
  });
});

describe("the checkout", () => {
  test("the picker lists this folder and the folder's ready worktrees only", async () => {
    noteWorktree(worktree({ branch: "viki/one" }));
    noteWorktree(worktree({ branch: "viki/removing", state: "removing" }));
    noteWorktree(worktree({ branch: "viki/kept", state: "kept" }));
    noteWorktree(worktree({ branch: "viki/elsewhere", workspace_id: OTHER }));
    await setup(stub());
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "This folder",
      "viki/one (worktree)",
    ]);
    expect(screen.getByLabelText("Checkout")).toBeTruthy();
  });

  test("the folder is reviewed first, with an abort signal", async () => {
    noteWorktree(worktree());
    const api = stub();
    await setup(api);
    expect(api.review).toHaveBeenCalledTimes(1);
    expect(api.review).toHaveBeenCalledWith(W, null, expect.any(AbortSignal));
  });

  test("initialWorktree preselects its worktree and reviews it", async () => {
    const wt = worktree({ branch: "viki/one" });
    noteWorktree(wt);
    const api = stub({
      review: vi.fn().mockResolvedValue(review({ worktree_id: wt.worktree_id })),
    });
    await setup(api, { initialWorktree: wt.worktree_id });
    expect((screen.getByLabelText("Checkout") as HTMLSelectElement).value).toBe(wt.worktree_id);
    expect(api.review).toHaveBeenCalledTimes(1);
    expect(api.review).toHaveBeenCalledWith(W, wt.worktree_id, expect.any(AbortSignal));
  });

  test("an initialWorktree that is not ready falls back to the folder", async () => {
    const gone = worktree({ state: "removing" });
    noteWorktree(gone);
    const api = stub();
    await setup(api, { initialWorktree: gone.worktree_id });
    expect(api.review).toHaveBeenCalledWith(W, null, expect.any(AbortSignal));
  });

  test("picking a worktree reviews it and drops the old review at once", async () => {
    const wt = worktree({ branch: "viki/one" });
    noteWorktree(wt);
    const second = deferred<ReviewJson>();
    const api = stub({
      review: vi
        .fn()
        .mockResolvedValueOnce(review())
        .mockImplementationOnce(() => second.promise),
    });
    await setup(api);
    expect(screen.getByText("src/a.ts")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Checkout"), { target: { value: wt.worktree_id } });
    await settle();
    expect(api.review).toHaveBeenLastCalledWith(W, wt.worktree_id, expect.any(AbortSignal));
    expect(screen.queryByText("src/a.ts")).toBeNull();
    second.resolve(review({ worktree_id: wt.worktree_id, entries: [text("src/c.ts")] }));
    await settle();
    expect(screen.getByText("src/c.ts")).toBeTruthy();
  });

  test("Refresh aborts the previous call and ignores its late answer", async () => {
    const first = deferred<ReviewJson>();
    const second = deferred<ReviewJson>();
    const api = stub({
      review: vi
        .fn()
        .mockImplementationOnce(() => first.promise)
        .mockImplementationOnce(() => second.promise),
    });
    await setup(api);
    const firstSignal = reviewCalls(api)[0]?.[2] as AbortSignal;
    expect(firstSignal.aborted).toBe(false);
    await click("Refresh");
    expect(firstSignal.aborted).toBe(true);
    first.resolve(review({ entries: [text("old.ts")] }));
    second.resolve(review({ entries: [text("new.ts")] }));
    await settle();
    expect(screen.queryByText("old.ts")).toBeNull();
    expect(screen.getByText("new.ts")).toBeTruthy();
  });

  test("an untrusted folder shows the trust words and asks nothing", async () => {
    const api = stub();
    await setup(api, { folder: folder({ policy: "existing_readonly" }) });
    expect(screen.getByText("Sessions start only in a trusted folder.")).toBeTruthy();
    expect(api.review).not.toHaveBeenCalled();
    expect(screen.queryByRole("button")).toBeNull();
  });

  test("a failed review shows its refusal words", async () => {
    const api = stub({ review: vi.fn().mockRejectedValue(new ReviewApiError("git_unavailable")) });
    await setup(api);
    expect(screen.getByRole("alert").textContent).toBe("Git could not do this. Try again later.");
  });
});

describe("the entries", () => {
  test("the branch line, the change words and a box for text entries only", async () => {
    await setup(stub());
    expect(screen.getByText(`Branch main at ${HEAD.slice(0, 12)}`)).toBeTruthy();
    expect(screen.getAllByRole("checkbox").map((box) => box.getAttribute("aria-label"))).toEqual([
      "Include src/a.ts",
      "Include src/b.ts",
    ]);
    expect(screen.getAllByText("Modified")).toHaveLength(2);
    expect(screen.getByText("Added")).toBeTruthy();
    expect(screen.getByText("Binary file: commit it in a terminal or VS Code.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "src/a.ts" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "logo.png" })).toBeNull();
  });

  test("every other kind gets its words and no box", async () => {
    const kinds: ReviewKind[] = [
      "symlink",
      "submodule",
      "nested_repository",
      "filtered",
      "private",
      "too_large",
      "unmerged",
      "unsupported_name",
      "other",
    ];
    await setup(
      stub({
        review: vi
          .fn()
          .mockResolvedValue(review({ entries: kinds.map((kind) => other(`p/${kind}`, kind)) })),
      }),
    );
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
    expect(screen.getByText("Link: commit it in a terminal.")).toBeTruthy();
    expect(screen.getByText("Private file: VIKI does not show or commit it.")).toBeTruthy();
    expect(screen.getByText("0 of 0 files selected")).toBeTruthy();
  });

  test("Select all and Clear change the count", async () => {
    await setup(stub());
    expect(screen.getByText("0 of 2 files selected")).toBeTruthy();
    await click("Select all");
    expect(screen.getByText("2 of 2 files selected")).toBeTruthy();
    expect(
      (screen.getByRole("checkbox", { name: "Include src/b.ts" }) as HTMLInputElement).checked,
    ).toBe(true);
    await click("Clear");
    expect(screen.getByText("0 of 2 files selected")).toBeTruthy();
    tick("src/a.ts");
    expect(screen.getByText("1 of 2 files selected")).toBeTruthy();
  });

  test("no changes and a truncated list have their words", async () => {
    const api = stub({
      review: vi.fn().mockResolvedValue(review({ entries: [], truncated: true })),
    });
    await setup(api);
    expect(screen.getByText("No changes to review.")).toBeTruthy();
    expect(
      screen.getByText(
        "More than 500 changes: only the first 500 are listed. Commit the rest in a terminal.",
      ),
    ).toBeTruthy();
    expect(screen.queryByLabelText("Commit message")).toBeNull();
  });

  test("a path that looks like markup stays text", async () => {
    const path = "<script>alert(1)</script>.ts";
    const api = stub({
      review: vi
        .fn()
        .mockResolvedValue(review({ entries: [text(path), other("<img src=x>", "binary")] })),
    });
    await setup(api);
    expect(screen.getByRole("button", { name: path })).toBeTruthy();
    expect(screen.getByText("<img src=x>")).toBeTruthy();
    expect(document.querySelector("script")).toBeNull();
    expect(document.querySelector("img")).toBeNull();
  });
});

describe("the view", () => {
  const view = (over: Partial<ReviewViewJson> = {}): ReviewViewJson => ({
    path: "src/a.ts",
    diff: null,
    content: null,
    reason: null,
    ...over,
  });

  test("a diff goes through the diff view, with the checkout and no range", async () => {
    const wt = worktree();
    noteWorktree(wt);
    const api = stub({
      review: vi.fn().mockResolvedValue(review({ worktree_id: wt.worktree_id })),
      view: vi
        .fn()
        .mockResolvedValue(
          view({ diff: "diff --git a/src/a.ts b/src/a.ts\n@@ -1 +1 @@\n-old\n+new\n" }),
        ),
    });
    await setup(api, { initialWorktree: wt.worktree_id });
    await click("src/a.ts");
    expect(api.view).toHaveBeenCalledWith(
      W,
      "src/a.ts",
      { worktreeId: wt.worktree_id, integration: false },
      expect.any(AbortSignal),
    );
    expect(screen.getByText("+new").className).toContain("diff-add");
    expect(screen.getByText("-old").className).toContain("diff-del");
  });

  test("a new file shows its content under New file", async () => {
    const api = stub({
      view: vi.fn().mockResolvedValue(view({ content: "<b>hello</b>\nline 2" })),
    });
    await setup(api);
    await click("src/a.ts");
    expect(screen.getByText("New file")).toBeTruthy();
    const block = screen.getByText(/hello/);
    expect(block.tagName).toBe("PRE");
    expect(block.textContent).toBe("<b>hello</b>\nline 2");
    expect(document.querySelector("b")).toBeNull();
  });

  test.each([
    ["binary", "Binary file: no text to show."],
    ["too_large", "Over 1 MiB: not shown."],
    ["no_diff", "No difference."],
    ["unsupported_encoding", "This file's encoding can't be shown."],
    ["no_commits", "This folder has no commit yet."],
  ] as const)("the %s reason has its words", async (reason, words) => {
    const api = stub({ view: vi.fn().mockResolvedValue(view({ reason })) });
    await setup(api);
    await click("src/a.ts");
    expect(screen.getByText(words)).toBeTruthy();
  });

  test("Loading… shows until the answer, a failure has its words, Back returns to the list", async () => {
    const pending = deferred<ReviewViewJson>();
    const api = stub({
      view: vi
        .fn()
        .mockImplementationOnce(() => pending.promise)
        .mockRejectedValueOnce(new ReviewApiError("private_path")),
    });
    await setup(api);
    await click("src/a.ts");
    expect(screen.getByText("Loading…")).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
    const back = screen.getByRole("button", { name: "Back to the list" });
    expect(document.activeElement).toBe(back);
    fireEvent.click(back);
    await settle();
    expect(screen.queryByText("Loading…")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "src/a.ts" }));
    pending.resolve(view({ content: "late" }));
    await settle();
    expect(screen.queryByText("late")).toBeNull();
    await click("src/b.ts");
    expect(screen.getByRole("alert").textContent).toBe("The change could not be loaded.");
  });

  describe("the patch download", () => {
    const DIFF = "diff --git a/src/a.ts b/src/a.ts\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n";
    const blobText = (blob: Blob) =>
      new Promise<string>((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.readAsText(blob);
      });
    function stubDownload() {
      const create = vi.fn((_blob: Blob) => "blob:patch");
      const revoke = vi.fn();
      vi.stubGlobal(
        "URL",
        Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }),
      );
      const saved: { href: string; download: string }[] = [];
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
        this: HTMLAnchorElement,
      ) {
        saved.push({ href: this.href, download: this.download });
      });
      return { create, revoke, saved };
    }
    const open = (path: string, over: Partial<ReviewViewJson>) =>
      stub({
        review: vi.fn().mockResolvedValue(review({ entries: [text(path)] })),
        view: vi.fn().mockResolvedValue(view({ path, ...over })),
      });

    afterEach(() => {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    });

    test("the viewed diff downloads as a patch", async () => {
      const { create, revoke, saved } = stubDownload();
      const api = open("src/a.ts", { diff: DIFF });
      await setup(api);
      await click("src/a.ts");
      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
      fireEvent.click(screen.getByRole("button", { name: "Download patch for src/a.ts" }));
      const blob = create.mock.calls[0]?.[0] as Blob;
      expect(blob.type).toBe("text/x-diff");
      expect(await blobText(blob)).toBe(DIFF);
      expect(saved).toEqual([{ href: "blob:patch", download: "a.ts.patch" }]);
      expect(revoke).not.toHaveBeenCalled();
      vi.advanceTimersByTime(999);
      expect(revoke).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1);
      expect(revoke).toHaveBeenCalledWith("blob:patch");
      expect(api.view).toHaveBeenCalledTimes(1);
      expect(document.querySelector("a[download]")).toBeNull();
    });

    test("a diff without a final newline is saved as given", async () => {
      const { create } = stubDownload();
      await setup(open("src/a.ts", { diff: "@@ -1 +1 @@\n+x" }));
      await click("src/a.ts");
      await click("Download patch for src/a.ts");
      expect(await blobText(create.mock.calls[0]?.[0] as Blob)).toBe("@@ -1 +1 @@\n+x");
    });

    test("the button says Download patch and names its file", async () => {
      stubDownload();
      await setup(open("src/a.ts", { diff: DIFF }));
      await click("src/a.ts");
      expect(screen.getByText("Download patch")).toBeTruthy();
      expect(screen.getByRole("button", { name: "Download patch for src/a.ts" })).toBeTruthy();
    });

    test("a root file keeps its whole name", async () => {
      const { saved } = stubDownload();
      await setup(open("README.md", { diff: DIFF }));
      await click("README.md");
      await click("Download patch for README.md");
      expect(saved[0]?.download).toBe("README.md.patch");
    });

    test("new-file content and a reason get no button", async () => {
      await setup(open("src/a.ts", { content: "hello" }));
      await click("src/a.ts");
      expect(screen.queryByText("Download patch")).toBeNull();
      cleanup();
      await setup(open("src/a.ts", { reason: "no_diff" }));
      await click("src/a.ts");
      expect(screen.queryByText("Download patch")).toBeNull();
    });
  });

  test("the selection survives a trip to the view", async () => {
    const api = stub({ view: vi.fn().mockResolvedValue(view({ diff: "@@ -1 +1 @@\n+x\n" })) });
    await setup(api);
    tick("src/b.ts");
    await click("src/a.ts");
    await click("Back to the list");
    expect(
      (screen.getByRole("checkbox", { name: "Include src/b.ts" }) as HTMLInputElement).checked,
    ).toBe(true);
  });
});

describe("the range view", () => {
  const withRange = (view: ReviewApi["view"], worktreeId: string, entries = [text("a.ts")]) =>
    stub({
      review: vi.fn().mockResolvedValue(
        review({
          worktree_id: worktreeId,
          branch: "viki/one",
          entries,
          integration: integration({
            paths: [
              { path: "src/range.ts", change: "modified", kind: "text" },
              { path: "logo.png", change: "added", kind: "binary" },
            ],
          }),
        }),
      ),
      view,
    });

  test("a text range path shows the integration diff, Back returns to the list", async () => {
    const wt = worktree({ branch: "viki/one" });
    noteWorktree(wt);
    const view = vi.fn().mockResolvedValue({
      path: "src/range.ts",
      diff: "diff --git a/src/range.ts b/src/range.ts\n@@ -1 +1 @@\n-old\n+new\n",
      content: null,
      reason: null,
    });
    const api = withRange(view, wt.worktree_id);
    await setup(api, { initialWorktree: wt.worktree_id });
    expect(screen.queryByRole("button", { name: "logo.png" })).toBeNull();
    await click("src/range.ts");
    expect(view).toHaveBeenCalledWith(
      W,
      "src/range.ts",
      { worktreeId: wt.worktree_id, integration: true },
      expect.any(AbortSignal),
    );
    expect(screen.getByText("+new").className).toContain("diff-add");
    expect(screen.queryByRole("checkbox")).toBeNull();
    const back = screen.getByRole("button", { name: "Back to the list" });
    expect(document.activeElement).toBe(back);
    await click("Back to the list");
    expect(screen.getByRole("checkbox", { name: "Include a.ts" })).toBeTruthy();
    expect(screen.queryByText("+new")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "src/range.ts" }));
  });

  test("it shows Loading…, a failure and a reason in the view's words", async () => {
    const wt = worktree({ branch: "viki/one" });
    noteWorktree(wt);
    const pending = deferred<ReviewViewJson>();
    const view = vi
      .fn()
      .mockImplementationOnce(() => pending.promise)
      .mockRejectedValueOnce(new ReviewApiError("private_path"))
      .mockResolvedValueOnce({
        path: "src/range.ts",
        diff: null,
        content: null,
        reason: "too_large",
      });
    await setup(withRange(view, wt.worktree_id), { initialWorktree: wt.worktree_id });
    await click("src/range.ts");
    expect(screen.getByText("Loading…")).toBeTruthy();
    await click("src/range.ts");
    expect(screen.getByRole("alert").textContent).toBe("The change could not be loaded.");
    await click("src/range.ts");
    expect(screen.getByText("Over 1 MiB: not shown.")).toBeTruthy();
    pending.resolve({
      path: "src/range.ts",
      diff: "@@ -1 +1 @@\n+late\n",
      content: null,
      reason: null,
    });
    await settle();
    expect(screen.queryByText("+late")).toBeNull();
  });
});

describe("the commit form", () => {
  test("commit sends the selected paths with their digests", async () => {
    const wt = worktree({ branch: "viki/one" });
    noteWorktree(wt);
    const api = stub({
      review: vi
        .fn()
        .mockResolvedValue(review({ worktree_id: wt.worktree_id, branch: "viki/one" })),
      commit: vi.fn().mockResolvedValue(committed()),
    });
    await setup(api, { initialWorktree: wt.worktree_id });
    tick("src/b.ts");
    write("Fix b");
    await click(COMMIT);
    expect(api.commit).toHaveBeenCalledTimes(1);
    expect(api.commit).toHaveBeenCalledWith(W, {
      policy_revision: 3,
      root_physical_id: "1:2",
      worktree_id: wt.worktree_id,
      branch: "viki/one",
      head_oid: HEAD,
      message: "Fix b",
      paths: [{ path: "src/b.ts", digest: D2 }],
    });
  });

  test("two selected paths go in the review's order, each with its own digest", async () => {
    const api = stub({ commit: vi.fn().mockResolvedValue(committed()) });
    await setup(api);
    tick("src/b.ts");
    tick("src/a.ts");
    write("Both");
    await click(COMMIT);
    const body = (api.commit as ReturnType<typeof vi.fn>).mock.calls[0]?.[1];
    expect(body.worktree_id).toBeNull();
    expect(body.paths).toEqual([
      { path: "src/a.ts", digest: D1 },
      { path: "src/b.ts", digest: D2 },
    ]);
  });

  test("a done commit says so, clears the box and the ticks, and reviews again", async () => {
    const api = stub({
      review: vi
        .fn()
        .mockResolvedValueOnce(review())
        .mockResolvedValueOnce(review({ head_oid: NEXT, entries: [text("src/b.ts", D2)] })),
      commit: vi.fn().mockResolvedValue(committed()),
    });
    await setup(api);
    tick("src/a.ts");
    write("Fix a");
    await click(COMMIT);
    expect(ownStatus().textContent).toBe(`Committed ${NEXT.slice(0, 12)}.`);
    expect(api.review).toHaveBeenCalledTimes(2);
    expect((screen.getByLabelText("Commit message") as HTMLTextAreaElement).value).toBe("");
    expect(screen.queryByText("src/a.ts")).toBeNull();
    expect(screen.getByText("0 of 1 files selected")).toBeTruthy();
    expect(screen.getByText(`Branch main at ${NEXT.slice(0, 12)}`)).toBeTruthy();
  });

  test("hook_changed and unexpected paths are reported as counts", async () => {
    const api = stub({
      commit: vi
        .fn()
        .mockResolvedValue(committed({ hook_changed: ["src/a.ts"], unexpected: ["x", "y"] })),
    });
    await setup(api);
    tick("src/a.ts");
    write("Fix a");
    await click(COMMIT);
    const status = ownStatus();
    expect(
      within(status).getByText(
        "A hook changed 1 of the files while committing. Review the commit before you integrate or push.",
      ),
    ).toBeTruthy();
    expect(
      within(status).getByText(
        "The commit also holds 2 files you did not select. Check it in a terminal.",
      ),
    ).toBeTruthy();
  });

  test("the button reads Committing… and is disabled while the call runs", async () => {
    const pending = deferred<CommitJson>();
    const api = stub({ commit: vi.fn().mockImplementation(() => pending.promise) });
    await setup(api);
    tick("src/a.ts");
    write("Fix a");
    await click(COMMIT);
    const busy = screen.getByRole("button", { name: "Committing…" }) as HTMLButtonElement;
    expect(busy.disabled).toBe(true);
    fireEvent.submit(busy.closest("form") as HTMLFormElement);
    expect(api.commit).toHaveBeenCalledTimes(1);
    pending.resolve(committed());
    await settle();
    expect(screen.getByRole("button", { name: COMMIT })).toBeTruthy();
  });

  test("a stale refusal shows its words and the review is read again", async () => {
    const api = stub({
      review: vi
        .fn()
        .mockResolvedValueOnce(review())
        .mockResolvedValueOnce(review({ head_oid: NEXT, entries: [text("src/a.ts", D2)] })),
      commit: vi.fn().mockRejectedValue(new ReviewApiError("stale_review")),
    });
    await setup(api);
    tick("src/a.ts");
    write("Fix a");
    await click(COMMIT);
    expect(screen.getByRole("alert").textContent).toBe(
      "This changed since you reviewed it. Refresh and review again.",
    );
    expect(api.review).toHaveBeenCalledTimes(2);
    expect(screen.getByText(`Branch main at ${NEXT.slice(0, 12)}`)).toBeTruthy();
    // The message stays for the next try; the ticks do not.
    expect((screen.getByLabelText("Commit message") as HTMLTextAreaElement).value).toBe("Fix a");
    expect(commitButton().disabled).toBe(true);
  });

  test("an unknown failure has the generic words, and Refresh clears a refusal", async () => {
    const api = stub({ commit: vi.fn().mockRejectedValue(new Error("boom")) });
    await setup(api);
    tick("src/a.ts");
    write("Fix a");
    await click(COMMIT);
    expect(screen.getByRole("alert").textContent).toBe("That did not work. Try again.");
    await click("Refresh");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("a commit answered after a checkout change is not shown", async () => {
    const wt = worktree({ branch: "viki/one" });
    noteWorktree(wt);
    const pending = deferred<CommitJson>();
    const api = stub({ commit: vi.fn().mockImplementation(() => pending.promise) });
    await setup(api);
    tick("src/a.ts");
    write("Fix a");
    await click(COMMIT);
    fireEvent.change(screen.getByLabelText("Checkout"), { target: { value: wt.worktree_id } });
    await settle();
    pending.resolve(committed());
    await settle();
    expect(ownStatus().textContent).toBe("");
  });

  test("a commit answered after a checkout change leaves the new checkout's draft", async () => {
    const wt = worktree({ branch: "viki/one" });
    noteWorktree(wt);
    const pending = deferred<CommitJson>();
    const api = stub({ commit: vi.fn().mockImplementation(() => pending.promise) });
    await setup(api);
    tick("src/a.ts");
    write("Fix a");
    await click(COMMIT);
    fireEvent.change(screen.getByLabelText("Checkout"), { target: { value: wt.worktree_id } });
    await settle();
    write("Draft for the worktree");
    pending.resolve(committed());
    await settle();
    expect((screen.getByLabelText("Commit message") as HTMLTextAreaElement).value).toBe(
      "Draft for the worktree",
    );
  });

  describe("each disabled reason", () => {
    test("nothing selected, then no message", async () => {
      await setup(stub());
      expect(commitButton().disabled).toBe(true);
      expect(form().getByText("Select at least one file.")).toBeTruthy();
      tick("src/a.ts");
      expect(form().getByText("Write a commit message.")).toBeTruthy();
      write("   ");
      expect(commitButton().disabled).toBe(true);
      write("Fix a");
      expect(commitButton().disabled).toBe(false);
      expect(commitButton().getAttribute("aria-describedby")).toBeNull();
    });

    test("no physical id", async () => {
      await setup(stub(), { folder: folder({}, null) });
      tick("src/a.ts");
      write("Fix a");
      expect(commitButton().disabled).toBe(true);
      expect(form().getByText("This folder can't be changed now.")).toBeTruthy();
    });

    test("detached head", async () => {
      const api = stub({
        review: vi.fn().mockResolvedValue(review({ branch: null })),
      });
      await setup(api);
      const words = "No branch is checked out here (detached HEAD). Commit in a terminal.";
      expect(screen.getByText(words)).toBeTruthy();
      tick("src/a.ts");
      write("Fix a");
      expect(commitButton().disabled).toBe(true);
      const described = document.getElementById(
        commitButton().getAttribute("aria-describedby") ?? "",
      );
      expect(described?.textContent).toBe(words);
      expect(screen.queryByRole("button", { name: /^Push/ })).toBeNull();
    });

    test("a merge in progress", async () => {
      const api = stub({ review: vi.fn().mockResolvedValue(review({ in_progress: true })) });
      await setup(api);
      const words =
        "A merge, rebase, cherry-pick or revert is in progress here. Finish it in a terminal first.";
      expect(screen.getAllByText(words)).toHaveLength(1);
      tick("src/a.ts");
      write("Fix a");
      expect(commitButton().disabled).toBe(true);
      const described = document.getElementById(
        commitButton().getAttribute("aria-describedby") ?? "",
      );
      expect(described?.textContent).toBe(words);
    });
  });
});

describe("integration and push", () => {
  test.each(["integrate", "push", "check"] as const)(
    "a late %s refreshes the current checkout and the next Commit names it",
    async (action) => {
      const first = worktree({ branch: "viki/first" });
      const second = worktree({ branch: "viki/second" });
      noteWorktree(first);
      noteWorktree(second);
      const pending = deferred<unknown>();
      const api = stub({
        review: vi.fn(async (_workspace: string, worktreeId: string | null) =>
          review({
            worktree_id: worktreeId,
            branch: worktreeId === first.worktree_id ? first.branch : second.branch,
            entries: [text(worktreeId === first.worktree_id ? "first.ts" : "second.ts")],
            integration: integration(),
            remote: "origin",
            remote_state: "ready",
          }),
        ),
        integrate: vi.fn().mockReturnValue(pending.promise),
        push:
          action === "check"
            ? vi.fn().mockResolvedValue({ outcome: "unknown", remote_oid: null })
            : vi.fn().mockReturnValue(pending.promise),
        check: vi.fn().mockReturnValue(pending.promise),
        commit: vi.fn().mockResolvedValue(committed()),
      });
      await setup(api, { initialWorktree: first.worktree_id });
      await click(action === "integrate" ? "Integrate into main" : "Push viki/first");
      if (action === "check") await click("Check again");
      fireEvent.change(screen.getByLabelText("Checkout"), {
        target: { value: second.worktree_id },
      });
      await settle();
      expect(screen.getByRole("button", { name: "second.ts" })).toBeTruthy();
      pending.resolve(
        action === "integrate"
          ? { outcome: "integrated", head_oid: NEXT }
          : { outcome: "pushed", remote_oid: HEAD },
      );
      await settle();
      expect(reviewCalls(api).map((args) => args[1])).toEqual([
        first.worktree_id,
        second.worktree_id,
        second.worktree_id,
      ]);
      expect((screen.getByLabelText("Checkout") as HTMLSelectElement).value).toBe(
        second.worktree_id,
      );
      expect(screen.queryByRole("button", { name: "first.ts" })).toBeNull();
      tick("second.ts");
      write("Approve the second checkout");
      await click(COMMIT);
      expect(api.commit).toHaveBeenCalledWith(
        W,
        expect.objectContaining({
          worktree_id: second.worktree_id,
          branch: second.branch,
          paths: [{ path: "second.ts", digest: D1 }],
        }),
      );
    },
  );

  test.each(["integrate", "push", "check"] as const)(
    "a late %s after unmount starts no review",
    async (action) => {
      const wt = worktree({ branch: "viki/one" });
      noteWorktree(wt);
      const pending = deferred<unknown>();
      const api = stub({
        review: vi.fn().mockResolvedValue(
          review({
            worktree_id: wt.worktree_id,
            branch: wt.branch,
            integration: integration(),
            remote: "origin",
            remote_state: "ready",
          }),
        ),
        integrate: vi.fn().mockReturnValue(pending.promise),
        push:
          action === "check"
            ? vi.fn().mockResolvedValue({ outcome: "unknown", remote_oid: null })
            : vi.fn().mockReturnValue(pending.promise),
        check: vi.fn().mockReturnValue(pending.promise),
      });
      await setup(api, { initialWorktree: wt.worktree_id });
      await click(action === "integrate" ? "Integrate into main" : "Push viki/one");
      if (action === "check") await click("Check again");
      cleanup();
      pending.resolve(
        action === "integrate"
          ? { outcome: "integrated", head_oid: NEXT }
          : { outcome: "pushed", remote_oid: HEAD },
      );
      await settle();
      expect(api.review).toHaveBeenCalledTimes(1);
    },
  );

  test("a worktree with an integration shows the panel, and a done integrate reviews again", async () => {
    const wt = worktree({ branch: "viki/one" });
    noteWorktree(wt);
    const api = stub({
      review: vi.fn().mockResolvedValue(
        review({
          worktree_id: wt.worktree_id,
          branch: "viki/one",
          integration: integration(),
        }),
      ),
      integrate: vi.fn().mockResolvedValue({ outcome: "integrated", head_oid: NEXT }),
    });
    await setup(api, { initialWorktree: wt.worktree_id });
    await click("Integrate into main");
    expect(api.integrate).toHaveBeenCalledWith(wt.worktree_id, {
      policy_revision: 3,
      root_physical_id: "1:2",
      target_branch: "main",
      target_oid: TARGET,
      head_oid: HEAD,
    });
    expect(screen.getByText(`Integrated: main is now at ${NEXT.slice(0, 12)}.`)).toBeTruthy();
    expect(api.review).toHaveBeenCalledTimes(2);
  });

  test("the folder's own review has no integration panel", async () => {
    await setup(stub());
    expect(screen.queryByRole("button", { name: /^Integrate/ })).toBeNull();
  });

  test("a branch shows the push panel, and a done push reviews again", async () => {
    const api = stub({
      review: vi
        .fn()
        .mockResolvedValue(review({ remote: "origin", remote_state: "ready", remote_oid: NEXT })),
      push: vi.fn().mockResolvedValue({ outcome: "pushed", remote_oid: HEAD }),
    });
    await setup(api);
    expect(screen.getByText(`Remote origin; last fetched at ${NEXT.slice(0, 12)}`)).toBeTruthy();
    await click("Push main");
    expect(api.push).toHaveBeenCalledWith(W, {
      policy_revision: 3,
      root_physical_id: "1:2",
      remote: "origin",
      branch: "main",
      head_oid: HEAD,
    });
    expect(screen.getByText("Pushed main to origin.")).toBeTruthy();
    expect(api.review).toHaveBeenCalledTimes(2);
  });

  test("the pull request page opens through onOpenUrl", async () => {
    const url = "https://github.com/o/r/compare/main...viki/one?quick_pull=1";
    const api = stub({
      review: vi.fn().mockResolvedValue(
        review({
          branch: "viki/one",
          remote: "origin",
          remote_state: "ready",
          pull_request_url: url,
        }),
      ),
    });
    const { onOpenUrl } = await setup(api);
    await click("Open pull request page");
    expect(onOpenUrl).toHaveBeenCalledWith(url);
  });
});
