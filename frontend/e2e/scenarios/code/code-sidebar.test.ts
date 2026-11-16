/** Local route fixtures exercise the real sidebar without reading a code folder. */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import type {
  Capabilities,
  SettingsView,
  WorkspaceJson,
  WorkspacesListing,
} from "../../../src/clients/app/shellApi";
import type {
  CodeChanges,
  CodeDiff,
  CodeFile,
  CodeSaveBody,
  CodeTree,
} from "../../../src/clients/code/codeApi";
import { appUrl, launchEdge, TEST_TOKEN } from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";
import { sidebarNavigation } from "../../sidebar-navigation";

const ACTIVE = "55555555-5555-4555-8555-555555555555";
const OTHER = "66666666-6666-4666-8666-666666666666";
const REF = "src/a b.ts";
const NEXT_REF = "src/a-long-file-name-that-stays-readable-in-the-narrow-code-sidebar.ts";
const DRAFT = "Keep this draft exactly.";
const AT = "2026-01-01T00:00:00Z";
let server: Server | undefined;
let browser: Browser | undefined;

beforeAll(async () => {
  server = await startServer({ fixture: "tests/fixtures/claude/text-two-turns.jsonl" });
  browser = await launchEdge();
}, 60_000);

afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

function folder(id: string, name: string): WorkspaceJson {
  return {
    workspace_id: id,
    kind: "code",
    root: `/synthetic/${name}`,
    root_physical_id: "1:2",
    root_available: true,
    aliased: false,
    name,
    policy: "ordinary",
    policy_revision: 1,
    revision: 1,
    source: "created",
    layout: "unknown",
    portable_id: null,
    identity: { established: false, git_root_oids: 0, inherits_from: null, probed_at: AT },
    face_dir: null,
    bootstrap: "none",
    persistent: true,
    created_at: AT,
    updated_at: AT,
  };
}

async function fixture(page: Page, fileText?: string, editable = false) {
  const calls: string[] = [];
  const sends: string[] = [];
  const escapes: string[] = [];
  const opens: string[] = [];
  const workspaceWrites: string[] = [];
  const saves: { workspaceId: string; path: string; body: CodeSaveBody }[] = [];
  const files = new Map<string, { text: string; sha256: string }>();
  for (const id of [ACTIVE, OTHER]) {
    files.set(`${id}/${REF}`, {
      text: fileText ?? "first line\nsecond line\n",
      sha256: "a".repeat(64),
    });
    files.set(`${id}/${NEXT_REF}`, { text: "export const other = 2;\n", sha256: "c".repeat(64) });
  }
  let fileHold: { started: () => void; released: Promise<void>; completed: () => void } | null =
    null;
  page.on("request", (request) => {
    if (
      new URL(request.url()).pathname.startsWith("/api/v2/workspaces") &&
      request.method() !== "GET"
    )
      workspaceWrites.push(request.method());
  });
  const alpha = folder(ACTIVE, "Alpha");
  const listing: WorkspacesListing = {
    workspaces: [alpha, folder(OTHER, "Beta")],
    active_id: ACTIVE,
    selected_id: ACTIVE,
    selected_by: "registration",
    chat_only: false,
    scratch: {
      workspace_id: "77777777-7777-4777-8777-777777777777",
      kind: "scratch",
      shared: true,
    },
  };
  await page.route("**/*", async (route) => {
    if (new URL(route.request().url()).origin === ORIGIN) await route.continue();
    else {
      escapes.push("http");
      await route.abort();
    }
  });
  page.on("websocket", (socket) => {
    if (new URL(socket.url()).host !== new URL(ORIGIN).host) escapes.push("websocket");
    socket.on("framesent", ({ payload }) => {
      const frame = JSON.parse(String(payload)) as { type?: string };
      if (frame.type === "turn.send") sends.push(frame.type);
      if (frame.type === "session.open") opens.push(frame.type);
    });
  });
  await page.route(`${ORIGIN}/api/v2/workspaces`, async (route) => {
    expect(route.request().headers().authorization).toBe(`Bearer ${TEST_TOKEN}`);
    expect(route.request().method()).toBe("GET");
    await route.fulfill({ json: listing });
  });
  await page.route(`${ORIGIN}/api/v2/capabilities`, async (route) => {
    expect(route.request().headers().authorization).toBe(`Bearer ${TEST_TOKEN}`);
    const response = await route.fetch();
    const capabilities = (await response.json()) as Capabilities;
    capabilities.workspace = {
      active: {
        id: ACTIVE,
        kind: "code",
        name: alpha.name,
        policy: alpha.policy,
        layout: "unknown",
        source: alpha.source,
        aliased: false,
        root_available: true,
      },
      selected_id: ACTIVE,
      selected_by: "registration",
      chat_only: false,
      shared_scratch: false,
      restart_required: false,
    };
    await route.fulfill({ response, json: capabilities });
  });
  await page.route(`${ORIGIN}/api/v2/settings`, async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    const settings = (await response.json()) as SettingsView;
    const values =
      settings.values === null
        ? null
        : Object.fromEntries(
            Object.entries(settings.values).filter(([key]) => key !== "layout.panes"),
          );
    await route.fulfill({
      response,
      json: {
        ...settings,
        effective: { ...settings.effective, "layout.panes": null },
        values,
      } satisfies SettingsView,
    });
  });
  await page.route(`${ORIGIN}/api/v2/vault-publications/recovery`, async (route) => {
    expect(route.request().headers().authorization).toBe(`Bearer ${TEST_TOKEN}`);
    expect(route.request().method()).toBe("GET");
    await route.fulfill({ json: { publications: [] } });
  });
  await page.route(`${ORIGIN}/api/v2/code/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    expect(request.headers().authorization).toBe(`Bearer ${TEST_TOKEN}`);
    const [id, operation] = url.pathname.slice("/api/v2/code/".length).split("/");
    expect([ACTIVE, OTHER]).toContain(id);
    const path = url.searchParams.get("path");
    if (request.method() === "PUT") {
      expect(editable).toBe(true);
      expect(operation).toBe("file");
      expect(path).toBe(REF);
      expect(url.search).toBe(`?path=${encodeURIComponent(REF)}`);
      expect(request.headers()["content-type"]).toBe("application/json");
      const body = request.postDataJSON() as CodeSaveBody;
      saves.push({ workspaceId: id ?? "", path: path ?? "", body });
      files.set(`${id}/${path}`, { text: body.content, sha256: "b".repeat(64) });
      await route.fulfill({
        json: {
          operation_id: "88888888-8888-4888-8888-888888888888",
          status: "saved",
          sha256: "b".repeat(64),
          cleanup_pending: false,
        },
      });
      return;
    }
    expect(request.method()).toBe("GET");
    expect(request.postData()).toBeNull();
    calls.push(`${id}/${operation}/${path ?? ""}`);
    let json: CodeTree | CodeFile | CodeChanges | CodeDiff;
    switch (operation) {
      case "tree":
        expect(["", "src"]).toContain(path);
        json = {
          path: path ?? "",
          entries: [
            {
              name: path === "src" ? "a b.ts" : "src",
              type: path === "src" ? "file" : "dir",
              size: path === "src" ? 22 : null,
            },
            ...(editable && path === "src"
              ? [{ name: NEXT_REF.slice("src/".length), type: "file" as const, size: 24 }]
              : []),
          ],
          truncated: false,
        };
        break;
      case "file": {
        expect(editable ? [REF, NEXT_REF] : [REF]).toContain(path);
        expect(url.search).toBe(`?path=${encodeURIComponent(path ?? "")}`);
        const source = files.get(`${id}/${path}`);
        if (!source) throw new Error("Unexpected synthetic file");
        json = {
          path: path ?? "",
          size: new TextEncoder().encode(source.text).byteLength,
          sha256: source.sha256,
          encoding: "utf-8",
          newline: "lf",
          text: source.text,
          reason: null,
        };
        break;
      }
      case "changes":
        expect(url.search).toBe("");
        json = {
          state: "ready",
          branch: "main",
          head: "abcdef123456",
          detached: false,
          upstream: null,
          ahead: null,
          behind: null,
          entries: [
            {
              path: REF,
              orig_path: null,
              index: ".",
              worktree: "M",
              kind: "tracked",
              private: false,
            },
          ],
          truncated: false,
        };
        break;
      case "diff":
        expect(path).toBe(REF);
        json = { path: REF, text: "@@ -1 +1 @@\n-first line\n+changed line\n", reason: null };
        break;
      default:
        throw new Error("Unexpected Code route");
    }
    const held = operation === "file" ? fileHold : null;
    if (held) {
      fileHold = null;
      held.started();
      await held.released;
    }
    try {
      await route.fulfill({ json });
    } catch (error) {
      if (request.failure()?.errorText !== "net::ERR_ABORTED") throw error;
    } finally {
      held?.completed();
    }
  });
  return {
    calls,
    saves,
    sends,
    opens,
    workspaceWrites,
    escapes,
    holdFile() {
      if (fileHold) throw new Error("a file reply is already held");
      let start!: () => void, release!: () => void, finish!: () => void;
      const started = new Promise<void>((done) => {
        start = done;
      });
      const released = new Promise<void>((done) => {
        release = done;
      });
      const completed = new Promise<void>((done) => {
        finish = done;
      });
      fileHold = { started: start, released, completed: finish };
      return { started, release, completed };
    },
  };
}

async function explorer(page: Page) {
  const toggle = page.getByRole("button", { name: "VIKI panel", exact: true });
  if ((await toggle.isVisible()) && (await toggle.getAttribute("aria-expanded")) !== "true")
    await toggle.click();
  const tabs = page.getByRole("tablist", { name: "Right sidebar", exact: true });
  await tabs.getByRole("tab", { name: "Code", exact: true }).click();
  const sidebar = page.getByRole("region", { name: "Code sidebar", exact: true });
  await sidebar.waitFor();
  return sidebar;
}

async function noPrimaryCode(page: Page) {
  const navigation = await sidebarNavigation(page);
  expect(await navigation.getByRole("button", { name: "Code", exact: true }).count()).toBe(0);
  const drawer = page.getByRole("dialog", { name: "Sessions and agents", exact: true });
  if (await drawer.isVisible())
    await drawer.getByRole("button", { name: "Close", exact: true }).click();
}

async function fits(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  expect(
    await page.evaluate(() => document.activeElement?.closest("[hidden], [inert]") === null),
  ).toBe(true);
}

async function walk(width: number) {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  try {
    const wire = await fixture(page);
    await page.goto(appUrl());
    const conversation = await page.locator('[data-region="conversation"]').elementHandle();
    const composer = page.locator('textarea[aria-label="Message"]');
    const editor = await composer.elementHandle();
    await composer.fill(DRAFT);
    await noPrimaryCode(page);
    expect(wire.calls).toEqual([]);
    const initialOpens = [...wire.opens];
    let sidebar = await explorer(page);
    const dir = sidebar.getByRole("button", { name: "src", exact: true });
    const folderIcon = dir.locator("svg");
    expect(await dir.getAttribute("aria-expanded")).toBe("false");
    expect(await folderIcon.getAttribute("aria-hidden")).toBe("true");
    expect(await folderIcon.getAttribute("focusable")).toBe("false");
    const closedPath = await folderIcon.locator("path").getAttribute("d");
    await dir.press("Enter");
    const file = sidebar.getByRole("button", { name: "a b.ts", exact: true });
    await file.waitFor();
    expect(await dir.getAttribute("aria-expanded")).toBe("true");
    expect(await folderIcon.locator("path").getAttribute("d")).not.toBe(closedPath);
    expect(await file.locator("svg").getAttribute("aria-hidden")).toBe("true");
    expect(await file.locator("svg").getAttribute("focusable")).toBe("false");
    expect(await file.locator("svg path").getAttribute("d")).not.toBe(closedPath);
    await dir.press("Space");
    await file.waitFor({ state: "detached" });
    expect(await folderIcon.locator("path").getAttribute("d")).toBe(closedPath);
    await dir.press("Enter");
    await file.press("Space");
    const center = page.locator('[data-region="center-pane"]');
    const content = center.getByRole("region", { name: "Code content", exact: true });
    await content.getByRole("listbox", { name: REF, exact: true }).waitFor();
    expect(await content.getByRole("heading", { name: REF, exact: true }).textContent()).toBe(REF);
    expect(await content.locator(".code-text").allTextContents()).toEqual([
      "first line",
      "second line",
    ]);
    expect(await content.locator(".code-gutter").allTextContents()).toEqual(["1", "2"]);
    expect(await page.locator(".code-sidebar .code-file, .code-sidebar .code-diff").count()).toBe(
      0,
    );
    expect(await composer.inputValue()).toBe(DRAFT);
    expect(await editor?.evaluate((node) => node.isConnected)).toBe(true);
    expect(
      await conversation?.evaluate(
        (node) => node.isConnected && node.hasAttribute("hidden") && node.hasAttribute("inert"),
      ),
    ).toBe(true);
    expect(await composer.isVisible()).toBe(false);
    if (width < 1280)
      expect(await page.getByRole("dialog", { name: "VIKI panel", exact: true }).count()).toBe(0);
    else expect(await sidebar.isVisible()).toBe(true);
    expect(await page.locator("section.conversation.pane .code-file-head").count()).toBe(0);
    await fits(page);

    sidebar = await explorer(page);
    await sidebar.getByRole("tab", { name: "Changes", exact: true }).click();
    await sidebar.getByRole("button", { name: `.M ${REF}`, exact: true }).click();
    await content.locator(".diff-add", { hasText: "+changed line" }).waitFor();
    expect(await content.getByRole("heading", { name: REF, exact: true }).textContent()).toBe(REF);
    expect(await composer.inputValue()).toBe(DRAFT);
    expect(await conversation?.evaluate((node) => node.isConnected)).toBe(true);
    await content.getByRole("button", { name: "File", exact: true }).click();
    await content
      .getByRole("listbox", { name: REF, exact: true })
      .getByRole("option")
      .first()
      .click();
    expect(
      await content
        .getByRole("button", { name: "Quote in chat", exact: true })
        .getAttribute("aria-disabled"),
    ).toBe("false");
    await content.getByRole("button", { name: "Quote in chat", exact: true }).click();
    await center.waitFor({ state: "detached" });
    expect(await composer.isVisible()).toBe(true);
    expect(await composer.evaluate((node) => node === document.activeElement)).toBe(true);
    const expectedDraft = `${DRAFT}\n\`${REF}:1\`\n`;
    expect(await composer.inputValue()).toBe(expectedDraft);

    sidebar = await explorer(page);
    await sidebar.getByRole("combobox").selectOption(OTHER);
    await sidebar.getByRole("tab", { name: "Files", exact: true }).click();
    await sidebar.getByRole("button", { name: "src", exact: true }).click();
    await sidebar.getByRole("button", { name: "a b.ts", exact: true }).click();
    await content.getByRole("listbox", { name: REF, exact: true }).waitFor();
    expect(
      await content
        .getByRole("button", { name: "Quote in chat", exact: true })
        .getAttribute("aria-disabled"),
    ).toBe("true");
    expect(await composer.inputValue()).toBe(expectedDraft);
    const preview = await content.elementHandle();
    sidebar = await explorer(page);
    expect(await sidebar.getByRole("combobox").inputValue()).toBe(OTHER);
    if (width < 1280) await sidebar.getByRole("button", { name: "src", exact: true }).click();
    expect(
      await sidebar
        .getByRole("button", { name: "a b.ts", exact: true })
        .getAttribute("aria-current"),
    ).toBe("true");
    const tabs = page.getByRole("tablist", { name: "Right sidebar", exact: true });
    await tabs.getByRole("tab", { name: "Overview", exact: true }).click();
    expect(await preview?.evaluate((node) => node.isConnected)).toBe(true);
    if (width < 1280) {
      await page
        .getByRole("dialog", { name: "VIKI panel", exact: true })
        .getByRole("button", { name: "Close", exact: true })
        .click();
      const remountedTree = page.waitForResponse(
        (response) => new URL(response.url()).pathname === `/api/v2/code/${OTHER}/tree`,
      );
      await page.setViewportSize({ width: 1440, height: 900 });
      await tabs.waitFor();
      await remountedTree;
    }
    expect(await preview?.evaluate((node) => node.isConnected)).toBe(true);
    await content.getByRole("button", { name: "Back to chat", exact: true }).click();
    await center.waitFor({ state: "detached" });
    expect(await composer.isVisible()).toBe(true);
    expect(await editor?.evaluate((node) => node.isConnected)).toBe(true);
    expect(
      await conversation?.evaluate(
        (node) => node.isConnected && !node.hasAttribute("hidden") && !node.hasAttribute("inert"),
      ),
    ).toBe(true);
    expect(
      await tabs
        .getByRole("tab", { name: "Code", exact: true })
        .evaluate((node) => node === document.activeElement),
    ).toBe(true);
    expect(await composer.inputValue()).toBe(expectedDraft);
    await fits(page);
    expect(wire.calls).toEqual([
      `${ACTIVE}/tree/`,
      `${ACTIVE}/tree/src`,
      `${ACTIVE}/file/${REF}`,
      ...(width < 1280 ? [`${ACTIVE}/tree/`] : []),
      `${ACTIVE}/changes/`,
      `${ACTIVE}/file/${REF}`,
      `${ACTIVE}/diff/${REF}`,
      ...(width < 1280 ? [`${ACTIVE}/tree/`] : []),
      `${OTHER}/tree/`,
      `${OTHER}/tree/src`,
      `${OTHER}/file/${REF}`,
      ...(width < 1280 ? [`${OTHER}/tree/`, `${OTHER}/tree/src`, `${OTHER}/tree/`] : []),
    ]);
    expect(wire.sends).toEqual([]);
    expect(wire.opens).toEqual(initialOpens);
    expect(wire.workspaceWrites).toEqual([]);
    expect(wire.escapes).toEqual([]);
  } finally {
    await page.close();
  }
}

test("the desktop explorer falls back to the center when pane space is below its minimum", async () => {
  await walk(1200);
});

test("the narrow explorer closes for the center, retains the other folder on reopen and returns docked", async () => {
  await walk(700);
});

test("the wide explorer keeps a file pane beside chat with exact tabs and an editable file", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1800, height: 900 } });
  try {
    const wire = await fixture(page, undefined, true);
    await page.goto(appUrl());
    const composer = page.locator('textarea[aria-label="Message"]');
    await composer.fill(DRAFT);
    const initialOpens = [...wire.opens];
    const sidebar = await explorer(page);
    expect(await page.locator('[data-region="left-rail"]').isVisible()).toBe(true);
    expect(await page.locator('[data-region="right-rail"]').isVisible()).toBe(true);
    await sidebar.locator(".workbench-tree").waitFor();
    expect(await sidebar.locator(".workbench-tree").isVisible()).toBe(true);
    await sidebar.getByRole("button", { name: "src", exact: true }).click();
    const firstFile = sidebar.getByRole("button", { name: "a b.ts", exact: true });
    const nextFile = sidebar.getByRole("button", {
      name: NEXT_REF.slice("src/".length),
      exact: true,
    });
    await firstFile.click();
    const filePane = page.locator("section.conversation.pane").filter({
      has: page.locator(".code-file-head"),
    });
    await filePane.locator(".code-file-head", { hasText: REF }).waitFor();
    const paneId = await filePane.getAttribute("data-pane");
    if (!paneId) throw new Error("The file pane must have an identity");
    const stableFilePane = page.locator(`section.conversation.pane[data-pane="${paneId}"]`);
    const leftRailBox = await page.locator('[data-region="left-rail"]').boundingBox();
    const rightRailBox = await page.locator('[data-region="right-rail"]').boundingBox();
    const filePaneBox = await filePane.boundingBox();
    if (!leftRailBox || !rightRailBox || !filePaneBox)
      throw new Error("The file pane and both rails must be visible");
    expect(filePaneBox.x).toBeGreaterThanOrEqual(leftRailBox.x + leftRailBox.width);
    expect(filePaneBox.x + filePaneBox.width).toBeLessThanOrEqual(rightRailBox.x);
    expect(rightRailBox.x + rightRailBox.width).toBeLessThanOrEqual(1800);
    const fileTabs = stableFilePane.locator(".session-tabs");
    const fileRefs = () =>
      fileTabs
        .locator(".session-tab-label")
        .evaluateAll((nodes) => nodes.map((node) => node.getAttribute("title")));
    await nextFile.click();
    await stableFilePane.locator(".code-file-head", { hasText: NEXT_REF }).waitFor();
    expect(await fileRefs()).toEqual([REF, NEXT_REF]);
    await firstFile.click();
    await stableFilePane.locator(".code-file-head", { hasText: REF }).waitFor();
    expect(await fileTabs.getByRole("tab").count()).toBe(2);
    expect(await fileRefs()).toEqual([REF, NEXT_REF]);
    expect(await composer.inputValue()).toBe(DRAFT);

    const edit = stableFilePane.getByRole("button", { name: "Edit", exact: true });
    await edit.click();
    const code = stableFilePane.locator(`.cm-content[aria-label="Edit ${REF}"]`);
    await code.waitFor();
    const editorBox = await stableFilePane.locator(".cm-editor").boundingBox();
    const sidebarBox = await sidebar.boundingBox();
    if (!editorBox || !sidebarBox) throw new Error("The editor and sidebar must be visible");
    expect(editorBox.width).toBeGreaterThan(200);
    expect(sidebarBox.x).toBeGreaterThanOrEqual(editorBox.x + editorBox.width);
    expect(await sidebar.locator(".workbench-tree").isVisible()).toBe(true);
    expect(await composer.inputValue()).toBe(DRAFT);
    await fits(page);
    expect(wire.calls).toEqual([
      `${ACTIVE}/tree/`,
      `${ACTIVE}/tree/src`,
      `${ACTIVE}/file/${REF}`,
      `${ACTIVE}/file/${NEXT_REF}`,
      `${ACTIVE}/file/${REF}`,
    ]);
    expect(wire.saves).toEqual([]);
    expect(wire.sends).toEqual([]);
    expect(wire.opens).toEqual(initialOpens);
    expect(wire.workspaceWrites).toEqual([]);
    expect(wire.escapes).toEqual([]);
  } finally {
    await page.close();
  }
});

test.each([1200, 700])(
  "Close before a held file response keeps the same chat and cannot reopen the center at %s px",
  async (width) => {
    if (!browser) throw new Error("Edge did not launch");
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    let held: { release: () => void } | undefined;
    try {
      const wire = await fixture(page);
      await page.goto(appUrl());
      const composer = page.locator('textarea[aria-label="Message"]');
      const chat = await page.locator('[data-region="conversation"]').elementHandle();
      const editor = await composer.elementHandle();
      await composer.fill(DRAFT);
      const initialOpens = [...wire.opens];
      const sidebar = await explorer(page);
      await sidebar.getByRole("button", { name: "src", exact: true }).click();
      const pending = wire.holdFile();
      held = pending;
      await sidebar.getByRole("button", { name: "a b.ts", exact: true }).click();
      await pending.started;
      const center = page.locator('[data-region="center-pane"]');
      await center.getByRole("status").filter({ hasText: "Loading file…" }).waitFor();
      expect(await composer.isVisible()).toBe(false);
      if (width < 1280)
        expect(await page.getByRole("dialog", { name: "VIKI panel", exact: true }).count()).toBe(0);
      await center.getByRole("button", { name: "Close code preview", exact: true }).click();
      await center.waitFor({ state: "detached" });
      expect(await composer.isVisible()).toBe(true);
      const restored =
        width < 1280
          ? page.getByRole("button", { name: "VIKI panel", exact: true })
          : sidebar.getByRole("button", { name: "a b.ts", exact: true });
      expect(await restored.evaluate((node) => node === document.activeElement)).toBe(true);
      pending.release();
      await pending.completed;
      await page.evaluate(
        () =>
          new Promise<void>((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
          ),
      );
      expect(await center.count()).toBe(0);
      expect(await composer.inputValue()).toBe(DRAFT);
      expect(await chat?.evaluate((node) => node.isConnected)).toBe(true);
      expect(await editor?.evaluate((node) => node.isConnected)).toBe(true);
      await fits(page);
      expect(wire.calls).toEqual([
        `${ACTIVE}/tree/`,
        `${ACTIVE}/tree/src`,
        `${ACTIVE}/file/${REF}`,
      ]);
      expect(wire.sends).toEqual([]);
      expect(wire.opens).toEqual(initialOpens);
      expect(wire.workspaceWrites).toEqual([]);
      expect(wire.escapes).toEqual([]);
    } finally {
      held?.release();
      await page.close();
    }
  },
);

const COLOR_SOURCE =
  'const message = "<b>hello</b>";\nfunction greet(name: string) {\n  // synthetic preview source\n  return message + name;\n}\n';
const COLOR_ROLES = [
  [".tok-keyword", "function"],
  [".tok-function", "greet"],
  [".tok-variableName", "message"],
  [".tok-string", '"<b>hello</b>"'],
] as const;

function channels(color: string): number[] {
  const match = /^rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)$/.exec(color);
  if (!match || Number(match[4] ?? "1") !== 1) throw new Error(`Expected opaque RGB: ${color}`);
  return match.slice(1, 4).map(Number);
}
function luminance(color: string): number {
  const rgb = channels(color).map((value) => {
    const channel = value / 255;
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * (rgb[0] ?? 0) + 0.7152 * (rgb[1] ?? 0) + 0.0722 * (rgb[2] ?? 0);
}
function contrast(foreground: string, background: string): number {
  const fg = luminance(foreground),
    bg = luminance(background);
  return (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
}

test("the center shows exact TypeScript with distinct syntax colors and readable selections in both themes", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({
    viewport: { width: 1200, height: 900 },
    colorScheme: "dark",
  });
  try {
    const wire = await fixture(page, COLOR_SOURCE);
    await page.goto(appUrl());
    const composer = page.locator('textarea[aria-label="Message"]');
    const chat = await page.locator('[data-region="conversation"]').elementHandle();
    const editor = await composer.elementHandle();
    await composer.fill(DRAFT);
    const initialOpens = [...wire.opens];
    const sidebar = await explorer(page);
    await sidebar.getByRole("button", { name: "src", exact: true }).click();
    await sidebar.getByRole("button", { name: "a b.ts", exact: true }).click();
    const center = page.locator('[data-region="center-pane"]');
    const content = center.getByRole("region", { name: "Code content", exact: true });
    const lines = content.getByRole("listbox", { name: REF, exact: true });
    await content.locator(".tok-function").first().waitFor();
    expect(await content.locator(".code-syntax").count()).toBe(1);
    const preview = await content.elementHandle();
    for (const theme of ["dark", "light"] as const) {
      await page.emulateMedia({ colorScheme: theme });
      await page.waitForFunction(
        (theme) => document.documentElement.dataset.theme === theme,
        theme,
      );
      expect(await content.locator(".code-text").allTextContents()).toEqual(
        COLOR_SOURCE.trimEnd().split("\n"),
      );
      expect(await content.locator(".code-gutter").allTextContents()).toEqual([
        "1",
        "2",
        "3",
        "4",
        "5",
      ]);
      expect(await content.locator(".tok-comment").textContent()).toBe(
        "// synthetic preview source",
      );
      expect(await content.locator("b, img, script").count()).toBe(0);
      expect(await content.getByRole("textbox").count()).toBe(0);
      expect(await content.getByRole("button", { name: "Edit", exact: true }).count()).toBe(1);
      expect(await content.getByRole("button", { name: "Save", exact: true }).count()).toBe(0);
      await lines.getByRole("option").last().click();
      const foregrounds: string[] = [];
      for (const [selector, text] of COLOR_ROLES) {
        const token = content.locator(selector).filter({ hasText: text }).first();
        expect(await token.textContent()).toBe(text);
        const foreground = await token.evaluate((node) => getComputedStyle(node).color);
        const rgb = channels(foreground);
        expect(Math.max(...rgb) - Math.min(...rgb)).toBeGreaterThan(20);
        foregrounds.push(foreground);
      }
      expect(new Set(foregrounds).size).toBe(4);
      await lines.getByRole("option").first().click();
      const selectedBackground = await lines
        .getByRole("option")
        .first()
        .evaluate((node) => getComputedStyle(node).backgroundColor);
      for (const [index, [selector, text]] of COLOR_ROLES.entries()) {
        const foreground = await content
          .locator(selector)
          .filter({ hasText: text })
          .first()
          .evaluate((node) => getComputedStyle(node).color);
        expect(foreground).toBe(foregrounds[index]);
        expect(contrast(foreground, selectedBackground)).toBeGreaterThanOrEqual(4.5);
      }
      const native = await content
        .locator(".tok-variableName")
        .filter({ hasText: "message" })
        .first()
        .evaluate((node) => {
          const range = document.createRange();
          range.selectNodeContents(node);
          const selection = window.getSelection();
          selection?.removeAllRanges();
          selection?.addRange(range);
          const style = getComputedStyle(node, "::selection");
          return {
            text: selection?.toString(),
            foreground: style.color,
            background: style.backgroundColor,
          };
        });
      expect(native.text).toBe("message");
      expect(contrast(native.foreground, native.background)).toBeGreaterThanOrEqual(4.5);
      expect(await preview?.evaluate((node) => node.isConnected)).toBe(true);
      expect(await chat?.evaluate((node) => node.isConnected)).toBe(true);
      expect(await editor?.evaluate((node) => node.isConnected)).toBe(true);
      expect(await composer.inputValue()).toBe(DRAFT);
      await fits(page);
    }
    await content.getByRole("button", { name: "Quote in chat", exact: true }).click();
    await center.waitFor({ state: "detached" });
    expect(await composer.inputValue()).toBe(`${DRAFT}\n\`${REF}:1\`\n`);
    expect(await composer.inputValue()).not.toContain("hello");
    expect(await composer.evaluate((node) => node === document.activeElement)).toBe(true);
    expect(wire.calls).toEqual([`${ACTIVE}/tree/`, `${ACTIVE}/tree/src`, `${ACTIVE}/file/${REF}`]);
    expect(wire.sends).toEqual([]);
    expect(wire.opens).toEqual(initialOpens);
    expect(wire.workspaceWrites).toEqual([]);
    expect(wire.escapes).toEqual([]);
  } finally {
    await page.close();
  }
});

test.each([1200, 700])(
  "explicit center editing saves and resumes a retained draft at %s px",
  async (width) => {
    if (!browser) throw new Error("Edge did not launch");
    const page = await browser.newPage({ viewport: { width, height: 900 }, colorScheme: "dark" });
    try {
      const wire = await fixture(page, COLOR_SOURCE, true);
      await page.goto(appUrl());
      const composer = page.locator('textarea[aria-label="Message"]');
      const chat = await page.locator('[data-region="conversation"]').elementHandle();
      const draft = await composer.elementHandle();
      await composer.fill(DRAFT);
      const initialOpens = [...wire.opens];
      let sidebar = await explorer(page);
      await sidebar.getByRole("button", { name: "src", exact: true }).click();
      await sidebar.getByRole("button", { name: "a b.ts", exact: true }).click();
      const center = page.locator('[data-region="center-pane"]');
      const content = center.getByRole("region", { name: "Code content", exact: true });
      await content.getByRole("listbox", { name: REF, exact: true }).waitFor();
      expect(await content.locator(".cm-editor").count()).toBe(0);
      expect(await content.getByRole("button", { name: "Save", exact: true }).count()).toBe(0);
      expect(wire.saves).toEqual([]);
      const edit = content.getByRole("button", { name: "Edit", exact: true });
      const pencil = edit.locator("svg");
      const editBox = await edit.boundingBox();
      const pencilBox = await pencil.boundingBox();
      expect(editBox?.height).toBe(32);
      expect(pencilBox?.width).toBe(16);
      expect(pencilBox?.height).toBe(16);
      expect(await pencil.getAttribute("aria-hidden")).toBe("true");
      expect(await pencil.getAttribute("focusable")).toBe("false");
      await edit.click();
      const code = content.locator(`.cm-content[aria-label="Edit ${REF}"]`);
      await code.locator(".tok-function").filter({ hasText: "greet" }).waitFor();
      await code.focus();
      expect(await code.evaluate((node) => node === document.activeElement)).toBe(true);
      const editorBox = await content.locator(".cm-editor").boundingBox();
      expect(editorBox?.width).toBeGreaterThan(200);
      expect(editorBox?.height).toBeGreaterThan(100);
      expect(await page.locator(".code-sidebar .cm-editor").count()).toBe(0);
      if (width >= 1280) {
        const sidebarBox = await sidebar.boundingBox();
        expect(await sidebar.locator(".workbench-tree").isVisible()).toBe(true);
        expect(sidebarBox?.x).toBeGreaterThanOrEqual((editorBox?.x ?? 0) + (editorBox?.width ?? 0));
      } else {
        expect(await page.getByRole("dialog", { name: "VIKI panel", exact: true }).count()).toBe(0);
      }
      const colors: string[] = [];
      for (const [selector, text] of COLOR_ROLES) {
        const token = code.locator(selector).filter({ hasText: text }).first();
        expect(await token.textContent()).toBe(text);
        const color = await token.evaluate((node) => getComputedStyle(node).color);
        const rgb = channels(color);
        expect(Math.max(...rgb) - Math.min(...rgb)).toBeGreaterThan(20);
        colors.push(color);
      }
      expect(new Set(colors).size).toBe(4);
      expect(await code.locator("b, img, script").count()).toBe(0);
      await page.keyboard.press("Control+End");
      await page.keyboard.type("// saved in center");
      const savedText = `${COLOR_SOURCE}// saved in center`;
      await content.getByRole("button", { name: "Save", exact: true }).click();
      await content.locator(".code-editor-status", { hasText: "Saved." }).waitFor();
      expect(wire.saves).toEqual([
        {
          workspaceId: ACTIVE,
          path: REF,
          body: {
            policy_revision: 1,
            root_physical_id: "1:2",
            content: savedText,
            expected_sha256: "a".repeat(64),
            encoding: "utf-8",
            newline: "lf",
          },
        },
      ]);
      await content.getByRole("button", { name: "Close", exact: true }).click();
      await content.getByRole("listbox", { name: REF, exact: true }).waitFor();
      expect(await content.locator(".code-text").allTextContents()).toEqual(savedText.split("\n"));
      expect(wire.calls.filter((call) => call === `${ACTIVE}/file/${REF}`)).toHaveLength(2);
      await expect
        .poll(() =>
          content
            .getByRole("button", { name: "Edit", exact: true })
            .evaluate((node) => node === document.activeElement),
        )
        .toBe(true);

      await content.getByRole("button", { name: "Edit", exact: true }).click();
      await code.waitFor();
      await code.focus();
      await page.keyboard.press("Control+End");
      await page.keyboard.type(" latest A draft");
      const dirtyText = `${savedText} latest A draft`;
      sidebar = await explorer(page);
      await sidebar
        .getByRole("group", { name: "Unsaved changes", exact: true })
        .getByRole("button", { name: REF, exact: true })
        .waitFor();
      expect(
        await sidebar.getByRole("option", { name: "Alpha (unsaved)", exact: true }).count(),
      ).toBe(1);
      const dir = sidebar.getByRole("button", { name: "src", exact: true });
      if ((await dir.getAttribute("aria-expanded")) !== "true") await dir.click();
      const next = sidebar.getByRole("button", {
        name: NEXT_REF.slice("src/".length),
        exact: true,
      });
      const geometry = await next.evaluate((node) => ({
        fits: node.scrollWidth <= node.clientWidth,
        iconWidth: node.querySelector("svg")?.getBoundingClientRect().width,
      }));
      expect(geometry.fits).toBe(true);
      expect(geometry.iconWidth).toBe(16);
      await next.press("Enter");
      await content.getByRole("listbox", { name: NEXT_REF, exact: true }).waitFor();
      expect(await content.locator(".cm-editor").count()).toBe(0);
      expect(await composer.inputValue()).toBe(DRAFT);
      sidebar = await explorer(page);
      await sidebar
        .getByRole("group", { name: "Unsaved changes", exact: true })
        .getByRole("button", { name: REF, exact: true })
        .click();
      await content.getByRole("listbox", { name: REF, exact: true }).waitFor();
      await content
        .getByRole("status")
        .filter({ hasText: "Unsaved changes are kept in this window." })
        .waitFor();
      expect(await content.locator(".cm-editor").count()).toBe(0);
      expect(await content.locator(".code-text").allTextContents()).toEqual(savedText.split("\n"));
      await content.getByRole("button", { name: "Edit", exact: true }).click();
      await expect
        .poll(() => code.locator(".cm-line").allTextContents())
        .toEqual(dirtyText.split("\n"));
      expect(wire.saves).toHaveLength(1);
      await content.getByRole("button", { name: "Back to chat", exact: true }).click();
      await center.waitFor({ state: "detached" });
      expect(await composer.isVisible()).toBe(true);
      expect(await composer.inputValue()).toBe(DRAFT);
      expect(await chat?.evaluate((node) => node.isConnected)).toBe(true);
      expect(await draft?.evaluate((node) => node.isConnected)).toBe(true);
      await fits(page);
      expect(wire.sends).toEqual([]);
      expect(wire.opens).toEqual(initialOpens);
      expect(wire.workspaceWrites).toEqual([]);
      expect(wire.escapes).toEqual([]);
    } finally {
      await page.close();
    }
  },
);
