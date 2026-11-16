/**
 * Editing in the Code workbench end-to-end: the editor loads on the first Edit and not before,
 * saves keep a file's encoding and line endings, the Python and plain-text Enter rules indent as
 * the language packs say (a tab file gets a tab), a file changed elsewhere raises a conflict with
 * a disk version, an unsaved edit survives a view change, and the window's unload prompt guards
 * it.
 *
 * The server starts with `git: true` for the Changes list; the repository is made after it is up,
 * under an empty global Git config
 * (see `e2e/scenarios/code/workbench.test.ts`). Files are written as raw bytes so that CRLF and
 * the BOM stay exactly as given.
 *
 * Every label asserted here is the `en` catalog row: the harness's Edge asks for English.
 *
 * Run from frontend/ (binds 8765; check it is free first, and build first):
 *   npm run e2e -- e2e/scenarios/code/edit.test.ts
 */
// The harness owns `e2e/node.d.ts`, which declares only what `server.ts` needs; these modules are
// this file's own business, so they are suppressed here rather than added to it
// (`e2e/scenarios/code/workbench.test.ts` does the same).
// @ts-expect-error see above
import { Buffer } from "node:buffer";
// @ts-expect-error see above
import { execFileSync } from "node:child_process";
// @ts-expect-error see above
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
// @ts-expect-error see above
import { homedir, tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
import type { Browser, Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, TEST_TOKEN } from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

/** `scripts/e2e_server.py`'s own SCRATCH root, mirrored as `e2e/scenarios/code/workbench.test.ts` does. */
const SCRATCH =
  process.platform === "win32" ? join(tmpdir(), "viki-e2e") : join(homedir(), ".cache", "viki-e2e");
const CODE = join(SCRATCH, "code");

let browser: Browser | undefined;
let server: Server | undefined;

beforeAll(async () => {
  browser = await launchEdge();
});

afterAll(async () => {
  await browser?.close();
});

afterEach(async () => {
  await server?.stop();
  server = undefined;
});

const utf16 = (text: string) =>
  Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(text, "utf16le")]);

/** A committed repository of synthetic files, each written byte for byte. */
function makeRepository(): void {
  mkdirSync(CODE, { recursive: true });
  const emptyConfig = join(SCRATCH, "empty.gitconfig");
  writeFileSync(emptyConfig, "", "utf-8");
  const env = { ...process.env, GIT_CONFIG_GLOBAL: emptyConfig, GIT_CONFIG_NOSYSTEM: "1" };
  const git = (...args: string[]) => execFileSync("git", ["-C", CODE, ...args], { env });
  git("init", "-q", "-b", "main");
  git("config", "user.email", "e2e@example.com");
  git("config", "user.name", "e2e");
  git("config", "core.autocrlf", "false");
  const put = (name: string, bytes: Uint8Array) => writeFileSync(join(CODE, name), bytes);
  put("a.txt", Buffer.from("first line\n"));
  put("crlf.txt", Buffer.from("one\r\ntwo\r\n"));
  put("utf16.txt", utf16("x\r\ny\r\n"));
  put("mixed.txt", Buffer.from("a\nb\r\n"));
  put("indent.py", Buffer.from("def f():"));
  put("notes.txt", Buffer.from("  item"));
  put("tabs.txt", Buffer.from("\titem"));
  git("add", "-A");
  git("commit", "-q", "-m", "scratch");
}

const read = (name: string) => readFileSync(join(CODE, name));

/** Use the sidebar's docked or drawer presentation at the current width. */
async function showView(page: Page, view: "Chat" | "Code"): Promise<void> {
  await navigateSidebar(page, view);
}

/** Settings → Workspaces and Vault → Add folders (typed path) → Trust…, acknowledging whatever
 * the panel asks. The typed field shows at once only where the OS folder picker is unavailable
 * (a headless runner); elsewhere it sits behind "Type a path instead". The server answers the
 * status the page read at load, so this branch never races that read. */
async function registerAndTrust(page: Page): Promise<void> {
  const picker = await page.request.get(`${ORIGIN}/api/v2/folders/pick`, {
    headers: { authorization: `Bearer ${TEST_TOKEN}` },
  });
  const { available } = (await picker.json()) as { available: boolean };
  await page.keyboard.press("Control+Comma");
  await page.waitForSelector('[data-region="settings"]');
  await page.getByRole("tab", { name: "Workspaces and Vault" }).click();
  const addFolders = page.locator(".ws-add");
  if (available) await addFolders.getByRole("button", { name: "Type a path instead" }).click();
  await addFolders.getByLabel("Code folder path").fill(CODE);
  await addFolders.getByRole("button", { name: "Add", exact: true }).click();
  await page.locator(".workspace-row .chip", { hasText: /^Not trusted$/ }).waitFor();
  await page.getByRole("button", { name: "Trust…" }).first().click();
  await page.getByRole("button", { name: "Trust this folder" }).click();
  const acknowledgements = page.locator(".policy-acknowledgements input[type=checkbox]");
  for (const box of await acknowledgements.all()) await box.check();
  await page.locator(".policy-acknowledgements").getByRole("button", { name: "Apply" }).click();
  await page.locator(".workspace-row .chip", { hasText: /^Trusted$/ }).waitFor();
}

/** Server up, repository made, folder trusted, the Code view showing: the page and its request URLs. */
async function begin(width: number): Promise<{ page: Page; requests: string[] }> {
  server = await startServer({ git: true });
  makeRepository();
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  const requests: string[] = [];
  page.on("request", (request) => requests.push(request.url()));
  await page.goto(appUrl());
  await page.waitForSelector('[data-region="shell"]');
  await registerAndTrust(page);
  await showView(page, "Code");
  await page
    .locator("#workbench-panel-files .workbench-tree")
    .getByRole("button", { name: "a.txt", exact: true })
    .waitFor();
  return { page, requests };
}

const editButton = (page: Page) => page.getByRole("button", { name: "Edit", exact: true });
const codemirrorRequests = (requests: string[]) =>
  requests.filter((url) => url.includes("/assets/codemirror-"));

/** Opens a file from the tree and waits for the viewer's Edit button. */
async function openFile(page: Page, name: string): Promise<void> {
  await page
    .locator("#workbench-panel-files .workbench-tree")
    .getByRole("button", { name, exact: true })
    .click();
  await editButton(page).waitFor();
}

/** Presses Edit and clicks into the editor, which renders after its grammar has loaded. */
async function startEditing(page: Page, name: string): Promise<void> {
  await editButton(page).click();
  const content = page.locator(`.cm-content[aria-label="Edit ${name}"]`);
  await content.waitFor();
  // The editor takes focus on its own once it is ready.
  await expect
    .poll(() => page.evaluate(() => document.activeElement?.closest(".cm-content") !== null))
    .toBe(true);
  await content.click();
  await page.keyboard.press("Control+End");
}

async function save(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Save", exact: true }).click();
}

async function saved(page: Page): Promise<void> {
  await page.locator(".code-editor-status", { hasText: "Saved." }).waitFor();
}

test("an edit keeps encodings and indents, conflicts, survives a view change and guards unload", async () => {
  const { page, requests } = await begin(1440);
  const dialogs: string[] = [];
  page.on("dialog", (dialog) => {
    dialogs.push(dialog.type());
    void dialog.dismiss();
  });

  // CRLF: the editor loads on the first Edit, not before; the file keeps its line endings.
  await openFile(page, "crlf.txt");
  expect(codemirrorRequests(requests)).toEqual([]);
  await startEditing(page, "crlf.txt");
  expect(codemirrorRequests(requests).length).toBeGreaterThan(0);
  await page.keyboard.type("three");
  await save(page);
  await saved(page);
  expect(read("crlf.txt").toString("utf-8")).toBe("one\r\ntwo\r\nthree");

  // UTF-16 with a BOM keeps its BOM and its code units.
  await openFile(page, "utf16.txt");
  await startEditing(page, "utf16.txt");
  await page.keyboard.type("z");
  await save(page);
  await saved(page);
  expect(read("utf16.txt").toString("hex")).toBe(utf16("x\r\ny\r\nz").toString("hex"));

  // Mixed line endings open read-only, with the reason beside Edit.
  await openFile(page, "mixed.txt");
  expect(await editButton(page).getAttribute("aria-disabled")).toBe("true");
  expect(await page.locator(".workbench-toggle .code-reason").textContent()).toContain(
    "mixes line endings",
  );

  // Python: the language pack highlights and Enter indents after the colon and keeps the body's.
  await openFile(page, "indent.py");
  await startEditing(page, "indent.py");
  await page.locator(".cm-content .tok-keyword").first().waitFor();
  await page.keyboard.press("Control+End");
  await page.keyboard.press("Enter");
  await page.keyboard.type("return 1");
  await page.keyboard.press("Enter");
  await page.keyboard.type("x");
  await save(page);
  await saved(page);
  expect(read("indent.py").toString("utf-8")).toBe("def f():\n    return 1\n    x");

  // No language: Enter keeps the line's indent.
  await openFile(page, "notes.txt");
  await startEditing(page, "notes.txt");
  await page.keyboard.press("Enter");
  await page.keyboard.type("next");
  await save(page);
  await saved(page);
  expect(read("notes.txt").toString("utf-8")).toBe("  item\n  next");

  // A file indented with a tab gets a tab.
  await openFile(page, "tabs.txt");
  await startEditing(page, "tabs.txt");
  await page.keyboard.press("Enter");
  await page.keyboard.type("next");
  await save(page);
  await saved(page);
  expect(read("tabs.txt").toString("utf-8")).toBe("\titem\n\tnext");

  // Escape inside the editor keeps the Code view, and Esc then Tab leaves the editor.
  const tabsEditor = page.locator('.cm-content[aria-label="Edit tabs.txt"]');
  await tabsEditor.click();
  await page.keyboard.press("Escape");
  await tabsEditor.waitFor();
  expect(await page.locator(".workbench").isVisible()).toBe(true);
  await page.keyboard.press("Tab");
  await expect
    .poll(() => page.evaluate(() => document.activeElement?.closest(".cm-editor") === null))
    .toBe(true);

  // Find opens in the editor, and Escape closes it without leaving the view.
  await tabsEditor.click();
  await page.keyboard.press("Control+f");
  const find = page.getByRole("textbox", { name: "Find" });
  await find.waitFor();
  await page.keyboard.press("Escape");
  await find.waitFor({ state: "detached" });
  await tabsEditor.waitFor();
  expect(await page.locator(".workbench").isVisible()).toBe(true);

  // Close (the buffer is clean after the save) puts the focus on the file's Edit button.
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await expect.poll(() => page.evaluate(() => document.activeElement?.textContent)).toBe("Edit");

  // Conflict: the file changes elsewhere after the edit began.
  await openFile(page, "a.txt");
  await startEditing(page, "a.txt");
  await page.keyboard.type(" mine");
  writeFileSync(join(CODE, "a.txt"), Buffer.from("external\n"));
  await save(page);
  await page
    .locator(".code-editor-message", {
      hasText: "This file changed elsewhere. Your edit is kept.",
    })
    .waitFor();
  const disk = page.locator(".code-editor-disk");
  await disk.getByRole("heading", { name: "Disk version" }).waitFor();
  expect((await disk.locator("pre").textContent())?.trim()).toBe("external");
  expect(read("a.txt").toString("utf-8")).toBe("external\n");

  // Navigation: the unsaved edit is held across a view change, with no dialog.
  await showView(page, "Chat");
  await showView(page, "Code");
  expect(dialogs).toEqual([]);
  const unsaved = page.locator(".workbench-unsaved");
  await unsaved.getByRole("button", { name: "a.txt", exact: true }).click();
  const content = page.locator('.cm-content[aria-label="Edit a.txt"]');
  await content.waitFor();
  expect(await content.textContent()).toContain("mine");

  await page.locator("#workbench-tab-changes").click();
  await page
    .locator("#workbench-panel-changes .workbench-changes")
    .getByRole("button", { name: /crlf\.txt/ })
    .waitFor();

  // At 700 px, with the editor open, the page does not scroll sideways.
  await page.setViewportSize({ width: 700, height: 900 });
  await content.waitFor();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          document.documentElement.scrollWidth <= window.innerWidth &&
          document.body.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);

  // The unload prompt guards the held edit; dismissing it keeps the page.
  // `page.close({ runBeforeUnload: true })` closes this Edge without raising the dialog (a plain
  // page with a beforeunload handler does the same), so the guard is reached by a reload.
  void page.reload().catch(() => {});
  await expect.poll(() => dialogs).toEqual(["beforeunload"]);
  await page.close();
}, 120_000);
