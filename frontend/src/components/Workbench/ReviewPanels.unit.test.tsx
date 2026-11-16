import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import {
  type IntegrationJson,
  type ReviewApi,
  ReviewApiError,
  type ReviewKind,
} from "../../clients/code/reviewApi";
import { changeWords, IntegrationPanel, kindWords, PushPanel, reviewRefusal } from "./ReviewPanels";
import { rootRefusal } from "./Worktrees";

const W = "00000000-0000-4000-8000-0000000000a1";
const WT = "00000000-0000-4000-8000-0000000000b1";
const HEAD = "1111111111111111111111111111111111111111";
const TARGET = "2222222222222222222222222222222222222222";
const LANDED = "3333333333333333333333333333333333333333";
const SEEN = "4444444444444444444444444444444444444444";
const PAGE = "https://github.com/o/r/compare/main...viki/b?quick_pull=1";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const folder = (rootId: string | null = "1:2") =>
  ({
    workspace_id: W,
    kind: "code",
    name: "Alpha",
    policy: "ordinary",
    policy_revision: 3,
    root_physical_id: rootId ?? undefined,
    root: "x",
    root_available: true,
  }) as WorkspaceJson;

const unused = () => Promise.reject(new Error("not stubbed"));
function stub(over: Partial<ReviewApi> = {}): ReviewApi {
  return {
    review: unused,
    view: unused,
    commit: unused,
    integrate: unused,
    push: unused,
    check: unused,
    ...over,
  };
}

const integration = (over: Partial<IntegrationJson> = {}): IntegrationJson => ({
  target_branch: "main",
  target_oid: TARGET,
  merge_base: TARGET,
  fast_forward: true,
  ahead: 2,
  behind: 0,
  commits: [{ oid: HEAD, subject: "Add the thing" }],
  commits_truncated: false,
  paths: [{ path: "src/a.ts", change: "modified", kind: "text" }],
  paths_truncated: false,
  ...over,
});

const settle = () => act(async () => {});
const click = (name: string | RegExp) => fireEvent.click(screen.getByRole("button", { name }));
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const REFUSAL_WORDS: [string, string][] = [
  ["stale_review", "This changed since you reviewed it. Refresh and review again."],
  ["stale_base", "The folder's branch moved since you reviewed it. Refresh and review again."],
  [
    "unrelated_staged",
    "Git already has other files staged here. Commit or unstage them in a terminal first.",
  ],
  [
    "in_progress",
    "A merge, rebase, cherry-pick or revert is in progress here. Finish it in a terminal first.",
  ],
  ["detached_head", "No branch is checked out here (detached HEAD). Commit in a terminal."],
  ["detached_target", "The folder has no branch checked out."],
  [
    "identity_missing",
    "Git doesn't know your name and email. Run git config --global user.name and git config --global user.email in a terminal, then try again.",
  ],
  [
    "commit_refused",
    "Git or a hook refused the commit, and the selected files stay staged. Run git commit in a terminal to see why.",
  ],
  ["unsupported_path", "A selected file can't be committed from VIKI. Refresh and review again."],
  ["too_many_paths", "Commit at most 200 files at a time."],
  ["invalid_message", "Write a commit message of up to 4,000 characters."],
  [
    "checkout_busy",
    "A session is working in this folder. Wait until its turn ends, then try again.",
  ],
  [
    "dirty_target",
    "The folder has uncommitted changes to tracked files. Commit or discard them first.",
  ],
  [
    "not_fast_forward",
    "The folder's branch has moved on, so this can't fast-forward. Merge or rebase in a terminal.",
  ],
  ["up_to_date", "Nothing to integrate."],
  ["unsupported_change", "These commits change files VIKI can't review. Integrate in a terminal."],
  [
    "merge_refused",
    "Git refused the fast-forward, so nothing changed. An untracked file may be in the way.",
  ],
  [
    "merge_failed",
    "The fast-forward stopped partway. Check the folder with git status in a terminal.",
  ],
  [
    "push_rejected",
    "The remote did not take the push. Its branch may have moved: fetch and integrate in a terminal.",
  ],
  ["push_failed", "VIKI could not reach the remote. Try again later."],
  ["no_remote", "This branch has no remote, and there is no origin."],
  [
    "remote_unsupported",
    "This remote pushes somewhere other than it fetches from, or to several places. Push in a terminal.",
  ],
  ["unsupported_branch", "VIKI can't use this branch name. Use a terminal."],
  ["worktree_moved", "This worktree changed. Refresh and try again."],
  [
    "unsupported_filesystem",
    "This folder's drive doesn't support commit, integrate and push from VIKI.",
  ],
  ["private_path", "VIKI can't use this file."],
  ["invalid_path", "VIKI can't use this file."],
  // The roots routes' catalog refusals reach the review panels through `rootRefusal`.
  [
    "git_settings_changed",
    "This folder's Git settings or hooks changed after a Bot copy was made, so VIKI Agent stopped its own Git actions here. Undo the change, or confirm the current settings in the Worktrees tab.",
  ],
  ["bot_copy_root", "This worktree belongs to a Bot chat. Open that chat instead."],
];

describe("words", () => {
  test.each(REFUSAL_WORDS)("%s has its words", (code, words) => {
    expect(reviewRefusal(code)).toBe(words);
  });

  test("any other code is the roots routes' words", () => {
    for (const code of ["trust_required", "no_commits", "vault_locked", "unavailable", "weird"]) {
      expect(reviewRefusal(code)).toBe(rootRefusal(code));
    }
  });

  test("kinds and changes have words, text has none", () => {
    expect(kindWords("text")).toBe("");
    expect(kindWords("symlink")).toBe("Link: commit it in a terminal.");
    expect(kindWords("nested_repository")).toBe(
      "A separate repository inside this folder: VIKI does not commit it.",
    );
    const kinds: ReviewKind[] = [
      "binary",
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
    for (const kind of kinds) {
      expect(kindWords(kind)).not.toBe("");
    }
    expect(changeWords("added")).toBe("Added");
    expect(changeWords("modified")).toBe("Modified");
    expect(changeWords("deleted")).toBe("Deleted");
  });
});

function integrationProps(over: Partial<Parameters<typeof IntegrationPanel>[0]> = {}) {
  return {
    api: stub(),
    folder: folder(),
    worktreeId: WT,
    headOid: HEAD,
    integration: integration(),
    onDone: vi.fn(),
    onOpenRange: vi.fn(),
    ...over,
  };
}

describe("IntegrationPanel", () => {
  test("a text range path opens its diff, any other path stays plain text", () => {
    const props = integrationProps({
      integration: integration({
        paths: [
          { path: "src/a.ts", change: "modified", kind: "text" },
          { path: "<b>logo</b>.png", change: "added", kind: "binary" },
        ],
      }),
    });
    render(<IntegrationPanel {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "src/a.ts" }));
    expect(props.onOpenRange).toHaveBeenCalledTimes(1);
    expect(props.onOpenRange).toHaveBeenCalledWith("src/a.ts");
    expect(screen.queryByRole("button", { name: "<b>logo</b>.png" })).toBeNull();
    expect(screen.getByText("<b>logo</b>.png")).toBeTruthy();
    expect(document.querySelector("b")).toBeNull();
  });

  test("its buttons are .btn inside .review-panel, where Review.css lets them wrap", () => {
    render(<IntegrationPanel {...integrationProps()} />);
    const button = screen.getByRole("button", { name: "Integrate into main" });
    expect(button.classList.contains("btn")).toBe(true);
    expect(button.closest(".review-panel")).not.toBeNull();
  });

  test("shows the branch, counts, commits, paths and the note", () => {
    render(
      <IntegrationPanel
        {...integrationProps({
          integration: integration({
            behind: 1,
            commits_truncated: true,
            paths: [
              { path: "src/a.ts", change: "modified", kind: "text" },
              { path: "logo.png", change: "added", kind: "binary" },
            ],
          }),
        })}
      />,
    );
    expect(screen.getByText("Folder branch main at 222222222222")).toBeTruthy();
    expect(screen.getByText("2 commits to integrate; 1 behind")).toBeTruthy();
    expect(screen.getByText("111111111111")).toBeTruthy();
    expect(screen.getByText("Add the thing")).toBeTruthy();
    expect(screen.getByText("Only the first 50 commits are listed.")).toBeTruthy();
    expect(
      screen.getByText("Uncommitted changes in the worktree are not integrated."),
    ).toBeTruthy();
    expect(screen.getByText("Binary file: commit it in a terminal or VS Code.")).toBeTruthy();
    expect(screen.getAllByText(/^Binary file/)).toHaveLength(1);
    expect(screen.getByText("Added")).toBeTruthy();
  });

  test("integrates with the reviewed oids, says so, then calls onDone", async () => {
    const pending = deferred<{ outcome: "integrated"; head_oid: string }>();
    const integrate = vi.fn().mockReturnValue(pending.promise);
    const props = integrationProps({ api: stub({ integrate }) });
    render(<IntegrationPanel {...props} />);
    click("Integrate into main");
    expect(integrate).toHaveBeenCalledTimes(1);
    expect(integrate.mock.calls[0]?.[0]).toBe(WT);
    expect(integrate.mock.calls[0]?.[1]).toEqual({
      policy_revision: 3,
      root_physical_id: "1:2",
      target_branch: "main",
      target_oid: TARGET,
      head_oid: HEAD,
    });
    const busy = screen.getByRole("button", { name: "Integrating…" }) as HTMLButtonElement;
    expect(busy.disabled).toBe(true);
    expect(props.onDone).not.toHaveBeenCalled();
    pending.resolve({ outcome: "integrated", head_oid: LANDED });
    await settle();
    expect(screen.getByRole("status").textContent).toBe("Integrated: main is now at 333333333333.");
    expect(props.onDone).toHaveBeenCalledTimes(1);
  });

  test("a refusal gets its words and no onDone", async () => {
    const integrate = vi.fn().mockRejectedValue(new ReviewApiError("stale_base"));
    const props = integrationProps({ api: stub({ integrate }) });
    render(<IntegrationPanel {...props} />);
    click("Integrate into main");
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(reviewRefusal("stale_base"));
    expect(screen.getByRole("alert").className).toBe("review-refusal");
    expect(props.onDone).not.toHaveBeenCalled();
    expect(
      (screen.getByRole("button", { name: "Integrate into main" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  test("an integrate refused for changed Git settings says the catalog sentence", async () => {
    const integrate = vi.fn().mockRejectedValue(new ReviewApiError("git_settings_changed"));
    const props = integrationProps({ api: stub({ integrate }) });
    render(<IntegrationPanel {...props} />);
    click("Integrate into main");
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(rootRefusal("git_settings_changed"));
    expect(screen.getByRole("alert").textContent).toMatch(/^This folder's Git settings or hooks/);
    expect(props.onDone).not.toHaveBeenCalled();
  });

  test("an answer without a code is the generic words", async () => {
    const integrate = vi.fn().mockRejectedValue(new Error("network"));
    render(<IntegrationPanel {...integrationProps({ api: stub({ integrate }) })} />);
    click("Integrate into main");
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(rootRefusal("unavailable"));
  });

  const reasons: [string, Partial<Parameters<typeof IntegrationPanel>[0]>, string][] = [
    ["no physical id", { folder: folder(null) }, "This folder can't be changed now."],
    ["no folder branch", { integration: integration({ target_branch: null }) }, "detached_target"],
    ["nothing ahead", { integration: integration({ ahead: 0 }) }, "up_to_date"],
    [
      "not a fast-forward",
      { integration: integration({ fast_forward: false, behind: 1 }) },
      "not_fast_forward",
    ],
    [
      "a path VIKI can't review",
      {
        integration: integration({
          paths: [{ path: "x.bin", change: "modified", kind: "binary" }],
        }),
      },
      "unsupported_change",
    ],
    [
      "too many paths",
      { integration: integration({ paths_truncated: true }) },
      "unsupported_change",
    ],
  ];
  test.each(reasons)("is disabled with its reason: %s", (_name, over, expected) => {
    const props = integrationProps({ api: stub({ integrate: vi.fn() }), ...over });
    render(<IntegrationPanel {...props} />);
    const button = screen.getByRole("button", { name: /^Integrate/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    const words = expected.includes(" ") ? expected : reviewRefusal(expected);
    expect(screen.getByText(words).className).toBe("review-reason");
    expect(button.getAttribute("aria-describedby")).toBe(screen.getByText(words).id);
    fireEvent.click(button);
    expect(props.api.integrate).not.toHaveBeenCalled();
  });

  test("a detached folder's button says Integrate", () => {
    render(
      <IntegrationPanel
        {...integrationProps({ integration: integration({ target_branch: null }) })}
      />,
    );
    expect(screen.getByRole("button", { name: "Integrate" })).toBeTruthy();
  });
});

function pushProps(over: Partial<Parameters<typeof PushPanel>[0]> = {}) {
  return {
    api: stub(),
    folder: folder(),
    branch: "viki/b",
    headOid: HEAD,
    remote: "origin",
    remoteState: "ready" as const,
    remoteOid: SEEN as string | null,
    pullRequestUrl: null as string | null,
    isWorktree: true,
    onOpenUrl: vi.fn(),
    onDone: vi.fn(),
    ...over,
  };
}

describe("PushPanel", () => {
  test("shows the remote line, last fetched or not on the remote", () => {
    const { rerender } = render(<PushPanel {...pushProps()} />);
    expect(screen.getByText("Remote origin; last fetched at 444444444444")).toBeTruthy();
    rerender(<PushPanel {...pushProps({ remoteOid: null })} />);
    expect(screen.getByText("Remote origin; not on the remote when last fetched")).toBeTruthy();
  });

  test("pushes the reviewed head, then says pushed and calls onDone", async () => {
    const pending = deferred<{ outcome: "pushed"; remote_oid: string }>();
    const push = vi.fn().mockReturnValue(pending.promise);
    const props = pushProps({ api: stub({ push }) });
    render(<PushPanel {...props} />);
    click("Push viki/b");
    expect(push).toHaveBeenCalledTimes(1);
    expect(push.mock.calls[0]?.[0]).toBe(W);
    expect(push.mock.calls[0]?.[1]).toEqual({
      policy_revision: 3,
      root_physical_id: "1:2",
      remote: "origin",
      branch: "viki/b",
      head_oid: HEAD,
    });
    expect((screen.getByRole("button", { name: "Pushing…" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    pending.resolve({ outcome: "pushed", remote_oid: HEAD });
    await settle();
    expect(screen.getByRole("status").textContent).toBe("Pushed viki/b to origin.");
    expect(props.onDone).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Check again" })).toBeNull();
  });

  test("a push refused for changed Git settings says the catalog sentence as an alert", async () => {
    const push = vi.fn().mockRejectedValue(new ReviewApiError("git_settings_changed"));
    render(<PushPanel {...pushProps({ api: stub({ push }) })} />);
    click("Push viki/b");
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(reviewRefusal("git_settings_changed"));
    expect(screen.getByRole("alert").textContent).toMatch(/^This folder's Git settings or hooks/);
  });

  test("an unknown push offers Check again", async () => {
    const push = vi.fn().mockResolvedValue({ outcome: "unknown", remote_oid: null });
    const props = pushProps({ api: stub({ push }) });
    render(<PushPanel {...props} />);
    click("Push viki/b");
    await settle();
    expect(screen.getByRole("status").textContent).toBe(
      "VIKI couldn't tell whether the push finished.",
    );
    expect(screen.getByRole("button", { name: "Check again" })).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Push viki/b" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(props.onDone).not.toHaveBeenCalled();
  });

  test("Check again finds the push landed", async () => {
    const push = vi.fn().mockResolvedValue({ outcome: "unknown", remote_oid: null });
    const check = vi.fn().mockResolvedValue({ outcome: "pushed", remote_oid: HEAD });
    const props = pushProps({ api: stub({ push, check }) });
    render(<PushPanel {...props} />);
    click("Push viki/b");
    await settle();
    click("Check again");
    await settle();
    expect(check).toHaveBeenCalledTimes(1);
    expect(check.mock.calls[0]?.[0]).toBe(W);
    expect(check.mock.calls[0]?.[1]).toEqual({
      remote: "origin",
      branch: "viki/b",
      head_oid: HEAD,
    });
    expect(screen.getByRole("status").textContent).toBe("Pushed viki/b to origin.");
    expect(props.onDone).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Check again" })).toBeNull();
  });

  test("Check again finds the push did not land", async () => {
    const push = vi.fn().mockResolvedValue({ outcome: "unknown", remote_oid: null });
    const check = vi.fn().mockResolvedValue({ outcome: "not_pushed", remote_oid: SEEN });
    const props = pushProps({ api: stub({ push, check }) });
    render(<PushPanel {...props} />);
    click("Push viki/b");
    await settle();
    click("Check again");
    await settle();
    expect(screen.getByRole("status").textContent).toBe(
      "The remote branch is not at this commit: the push did not land.",
    );
    expect(props.onDone).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Check again" })).toBeNull();
    expect(
      (screen.getByRole("button", { name: "Push viki/b" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  test("a refused check keeps Check again", async () => {
    const push = vi.fn().mockResolvedValue({ outcome: "unknown", remote_oid: null });
    const check = vi.fn().mockRejectedValue(new ReviewApiError("push_failed"));
    render(<PushPanel {...pushProps({ api: stub({ push, check }) })} />);
    click("Push viki/b");
    await settle();
    click("Check again");
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(reviewRefusal("push_failed"));
    expect(
      (screen.getByRole("button", { name: "Check again" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  test("a refused push gets its words and can be tried again", async () => {
    const push = vi.fn().mockRejectedValue(new ReviewApiError("push_rejected"));
    const props = pushProps({ api: stub({ push }) });
    render(<PushPanel {...props} />);
    click("Push viki/b");
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(reviewRefusal("push_rejected"));
    expect(props.onDone).not.toHaveBeenCalled();
    expect(
      (screen.getByRole("button", { name: "Push viki/b" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  const disabled: [string, Partial<Parameters<typeof PushPanel>[0]>, string][] = [
    ["no physical id", { folder: folder(null) }, "This folder can't be changed now."],
    ["no remote", { remote: null, remoteState: "none" }, reviewRefusal("no_remote")],
    ["an unsupported remote", { remoteState: "unsupported" }, reviewRefusal("remote_unsupported")],
  ];
  test.each(disabled)("is disabled with its words: %s", (_name, over, words) => {
    const props = pushProps({ api: stub({ push: vi.fn() }), ...over });
    render(<PushPanel {...props} />);
    const button = screen.getByRole("button", { name: "Push viki/b" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText(words)).toBeTruthy();
    fireEvent.click(button);
    expect(props.api.push).not.toHaveBeenCalled();
  });

  test("the page button opens the pull request URL", () => {
    const props = pushProps({ pullRequestUrl: PAGE });
    render(<PushPanel {...props} />);
    click("Open pull request page");
    expect(props.onOpenUrl).toHaveBeenCalledExactlyOnceWith(PAGE);
    expect(screen.queryByText(/doesn't know this host/)).toBeNull();
  });

  test("a worktree without a page says so, the folder says nothing", () => {
    const { rerender } = render(<PushPanel {...pushProps()} />);
    expect(screen.queryByRole("button", { name: "Open pull request page" })).toBeNull();
    expect(
      screen.getByText("VIKI doesn't know this host's pull request page. Open it on your host."),
    ).toBeTruthy();
    rerender(<PushPanel {...pushProps({ isWorktree: false })} />);
    expect(screen.queryByText(/doesn't know this host/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Open pull request page" })).toBeNull();
  });
});

describe("server strings stay text", () => {
  test("a script subject and path in the integration panel", () => {
    const { container } = render(
      <IntegrationPanel
        {...integrationProps({
          integration: integration({
            target_branch: "<script>alert(1)</script>",
            commits: [{ oid: HEAD, subject: "<script>alert(2)</script>" }],
            paths: [{ path: "<script>alert(3)</script>.ts", change: "added", kind: "text" }],
          }),
        })}
      />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("<script>alert(2)</script>")).toBeTruthy();
    expect(screen.getByText("<script>alert(3)</script>.ts")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Integrate into <script>alert(1)</script>" }),
    ).toBeTruthy();
  });

  test("a script branch and remote in the push panel", () => {
    const { container } = render(
      <PushPanel {...pushProps({ branch: "<script>alert(1)</script>", remote: "<b>r</b>" })} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByRole("button", { name: "Push <script>alert(1)</script>" })).toBeTruthy();
    expect(screen.getByText("Remote <b>r</b>; last fetched at 444444444444")).toBeTruthy();
  });
});
