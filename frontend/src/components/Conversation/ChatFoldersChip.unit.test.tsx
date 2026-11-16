/**
 * ChatFoldersChip: the "+N folder" chip's own mechanics — hidden with no extra folders, its
 * singular/plural trigger text, and the menu's vault row, folder rows (a registered name or the
 * last path segment) and Manage item. Opens the real Radix dropdown at most twice in this file:
 * many opens in one jsdom document can flood vitest's worker RPC.
 */
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ChatFoldersChip } from "./ChatFoldersChip";

afterEach(cleanup);

const VAULT = { name: "My Vault", root: "C:\\work\\my-app" };
const OTHER = "C:\\work\\other"; // no registered name: falls back to "other"
const NAMED = "C:\\work\\named";
const NAMES = new Map([[NAMED, "Named Folder"]]);

test("no extra folders: renders nothing, even with a known vault", () => {
  const { container } = render(
    <ChatFoldersChip folders={[]} vault={VAULT} names={NAMES} onManage={vi.fn()} />,
  );
  expect(container.firstChild).toBeNull();
});

test("one folder: the trigger reads +1 folder", () => {
  render(<ChatFoldersChip folders={[OTHER]} vault={VAULT} names={NAMES} onManage={vi.fn()} />);
  expect(screen.getByRole("button", { name: "+1 folder" })).toBeTruthy();
});

test("several folders: the trigger reads +N folders", () => {
  render(
    <ChatFoldersChip
      folders={[OTHER, NAMED, "C:\\work\\c"]}
      vault={VAULT}
      names={NAMES}
      onManage={vi.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "+3 folders" })).toBeTruthy();
});

test("the open menu lists the title, the vault row, each folder, and Manage, then picks Manage", () => {
  const onManage = vi.fn();
  render(
    <ChatFoldersChip folders={[OTHER, NAMED]} vault={VAULT} names={NAMES} onManage={onManage} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "+2 folders" }));
  const menu = screen.getByRole("menu");
  // Radix points the menu's own aria-labelledby at the trigger, which wins the computed
  // accessible name over aria-label — asserting the attribute directly instead of through
  // getByRole's name match proves this chip still sets it.
  expect(menu.getAttribute("aria-label")).toBe("Folders this chat can work in");
  // The panel's own class, so its "This chat can work in" label gets the padding
  // `.chat-folders-menu .lbl` (ChatFoldersChip.css) gives it, and the separator before Manage
  // reads as a visible hairline rather than a zero-height default.
  expect(menu.className).toContain("chat-folders-menu");
  expect(menu.querySelector(".chat-folders-sep")).not.toBeNull();

  expect(within(menu).getByText("This chat can work in")).toBeTruthy();
  expect(within(menu).getByText(`${VAULT.name} · starts here`)).toBeTruthy();
  expect(within(menu).getByText(VAULT.root)).toBeTruthy();

  // No registered name for OTHER: falls back to its last path segment.
  expect(within(menu).getByText("other")).toBeTruthy();
  expect(within(menu).getByText(OTHER)).toBeTruthy();
  // A registered name wins over the last segment.
  expect(within(menu).getByText("Named Folder")).toBeTruthy();
  expect(within(menu).getByText(NAMED)).toBeTruthy();

  const manageItem = within(menu).getByRole("menuitem", { name: "Manage workspaces in Settings" });
  fireEvent.click(manageItem);
  expect(onManage).toHaveBeenCalledTimes(1);
});

test("no known vault: the menu skips the vault row but still lists the folder", () => {
  render(<ChatFoldersChip folders={[OTHER]} vault={null} names={NAMES} onManage={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "+1 folder" }));
  const menu = screen.getByRole("menu");
  expect(within(menu).queryByText(/starts here/)).toBeNull();
  expect(within(menu).getByText("other")).toBeTruthy();
});

const COPY = {
  path: "C:\\data\\worktrees\\abc",
  folder: "my-app",
  branch: "viki/bot-20261112-abc123",
};
const HOSTILE = { path: "C:\\x\\<img src=x>", folder: "<b>app</b>", branch: "viki/<script>" };

function description(): string | null | undefined {
  const trigger = screen.getByRole("button");
  return document.getElementById(trigger.getAttribute("aria-describedby") as string)?.textContent;
}

test("a code copy alone renders the chip, counts as one, and is named in the description", () => {
  render(
    <ChatFoldersChip folders={[]} vault={VAULT} names={NAMES} codeCopy={COPY} onManage={vi.fn()} />,
  );
  expect(screen.getByRole("button", { name: "+1 folder" })).toBeTruthy();
  expect(description()).toBe(
    "This chat can work in: My Vault · starts here and Code copy of my-app · viki/bot-20261112-abc123",
  );
});

test("a code copy beside ordinary folders adds one to the count and the description", () => {
  render(
    <ChatFoldersChip
      folders={[OTHER, NAMED]}
      vault={null}
      names={NAMES}
      codeCopy={COPY}
      onManage={vi.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "+3 folders" })).toBeTruthy();
  expect(description()).toBe(
    "This chat can work in: Code copy of my-app · viki/bot-20261112-abc123, other, and Named Folder",
  );
});

test("the menu shows the copy and its path as text, hostile labels included", () => {
  render(
    <ChatFoldersChip
      folders={[OTHER]}
      vault={null}
      names={NAMES}
      codeCopy={HOSTILE}
      onManage={vi.fn()}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "+2 folders" }));
  const menu = screen.getByRole("menu");
  expect(within(menu).getByText("Code copy of <b>app</b> · viki/<script>")).toBeTruthy();
  expect(within(menu).getByText(HOSTILE.path)).toBeTruthy();
  expect(menu.querySelector("b, script, img")).toBeNull();
  expect(within(menu).getAllByRole("menuitem")).toHaveLength(1);
});

test("the trigger's accessible description lists the title, the vault, and each folder", () => {
  // Never opens the menu: a screen reader reads this off the closed trigger itself, and this
  // file's own budget (at most twice) is already spent by the two tests above that do open it.
  render(
    <ChatFoldersChip folders={[OTHER, NAMED]} vault={VAULT} names={NAMES} onManage={vi.fn()} />,
  );
  const trigger = screen.getByRole("button", { name: "+2 folders" });
  const descId = trigger.getAttribute("aria-describedby");
  expect(descId).toBeTruthy();
  const description = document.getElementById(descId as string);
  expect(description?.className).toContain("sr-only");
  expect(description?.textContent).toBe(
    "This chat can work in: My Vault · starts here, other, and Named Folder",
  );
});
