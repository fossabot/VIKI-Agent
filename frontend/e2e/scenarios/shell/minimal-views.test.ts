/** Populated view layouts with synthetic REST/WebSocket data; no backend or screenshots. */
import type { Browser, Locator, Page } from "playwright-core";
import { createServer, type ViteDevServer } from "vite";
import { afterAll, beforeAll, expect, test } from "vitest";
import { GENERATION, sessionRow, snapshotData } from "../../../src/accept/m3b/fakes";
import type {
  RoutineDetailData,
  RoutineItem,
  RoutineRun,
} from "../../../src/clients/automation/routinesApi";
import {
  blankProfileDocument,
  type ProfileSummary,
} from "../../../src/clients/profiles/profileApi";
import type { DisplayMetadata } from "../../../src/state/noteMetadata";
import {
  capabilitiesFixture,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../../src/testShell";
import { launchEdge } from "../../harness";
import { navigateSidebar } from "../../sidebar-navigation";

const AT = "2026-10-12T00:00:00.000Z";
const NEXT = "2026-10-13T00:00:00.000Z";
const SESSION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const PROFILE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const ROUTINE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const REFS = [
  `library/${"projects/".repeat(8)}${"a".repeat(120)}.md`,
  `library/${"projects/".repeat(8)}${"b".repeat(120)}.md`,
];
const PURPOSE = `Review synthetic notes with exact references. ${"Unbroken".repeat(20)}`;
const PROFILE_NAME = `Synthetic assistant ${"W".repeat(80)}`;
const ROUTINE_NAME = `Synthetic scheduled review ${"W".repeat(80)}`;
const ROUTINE_PURPOSE = "Review synthetic notes before the daily meeting.";
const MODEL = "synthetic-model";
const INACTIVE_WARNING = "This workspace is not currently active, so a run cannot start.";
const workspace = workspaceFixture({
  root: "/synthetic/vault",
  name: "Synthetic library",
  policy: "existing_readonly",
});
const routineWorkspace = workspaceFixture({
  workspace_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  root: "/synthetic/review-vault",
  name: "Synthetic review library",
  policy: "existing_readonly",
});
const profileDocument = blankProfileDocument();
profileDocument.identity = {
  ...profileDocument.identity,
  name: PROFILE_NAME,
  purpose: PURPOSE,
};
profileDocument.runtime = { ...profileDocument.runtime, model: MODEL, permission_mode: "plan" };
const profile: ProfileSummary = {
  profile_id: PROFILE,
  revision: 1,
  current_revision: 1,
  state_revision: 1,
  created_at: AT,
  updated_at: AT,
  archived: false,
  identity: profileDocument.identity,
  runtime: profileDocument.runtime,
  provenance: profileDocument.provenance,
};
const metadata: DisplayMetadata = {
  state: "ready",
  frontmatter_state: "ready",
  heading_state: "absent",
  display_title: "Shared title",
  title_source: "title",
  tags: ["synthetic", "reference-reading"],
  type: "note",
  status: "draft",
};
let browser: Browser | undefined;
let server: ViteDevServer | undefined;
let baseUrl = "";

beforeAll(async () => {
  server = await createServer({
    server: { host: "127.0.0.1", port: 0, strictPort: true },
    logLevel: "silent",
  });
  await server.listen();
  baseUrl = server.resolvedUrls?.local[0] ?? "";
  if (!baseUrl) throw new Error("Synthetic view server has no URL");
  browser = await launchEdge();
});

afterAll(async () => {
  await browser?.close();
  await server?.close();
});

async function open(width: number, fontScale: number) {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({
    viewport: { width, height: 900 },
    reducedMotion: "reduce",
    timezoneId: "UTC",
  });
  let metadataReads = 0;
  const noteReads: string[] = [];
  let waiting: (() => void) | undefined;
  const item = (
    routine_id: string,
    name: string,
    state: RoutineItem["state"],
    next_run: string | null = null,
  ): RoutineItem => ({
    routine_id,
    name,
    state,
    current_revision: 1,
    state_revision: 1,
    schedule: { kind: next_run ? "daily" : "manual", timezone: next_run ? "Asia/Tokyo" : null },
    next_run,
    last_run: null,
    last_attempt: null,
    active_run: null,
  });
  const blockedRun: RoutineRun = {
    occurrence_id: "synthetic-blocked-occurrence",
    kind: "manual",
    scheduled_for: null,
    trigger_id: null,
    revision: 1,
    state: "finished",
    reason: null,
    recorded_at: AT,
    attempt: {
      attempt_id: "synthetic-blocked-attempt",
      session_id: "synthetic-blocked-session",
      state: "finished",
      outcome: "blocked",
      reason: "profile_missing",
      profile_id: PROFILE,
      profile_revision: 1,
      native_version: null,
      created_at: AT,
      dispatched_at: null,
      finished_at: AT,
    },
  };
  const scheduled = item(ROUTINE, ROUTINE_NAME, "enabled", NEXT);
  scheduled.active_run = {
    attempt_id: "synthetic-active-attempt",
    session_id: SESSION,
    state: "running",
    stop_by: NEXT,
  };
  const routines = [
    {
      ...item("blocked", "Synthetic blocked review", "enabled"),
      last_run: blockedRun,
      last_attempt: blockedRun,
    },
    scheduled,
    item("manual", "Synthetic manual review", "tested"),
    item("paused", "Synthetic paused review", "paused"),
    item("archived", "Synthetic archived review", "archived"),
  ];
  await page.addInitScript((id) => sessionStorage.setItem("viki.selected", id), SESSION);
  await page.routeWebSocket("**/ws", (socket) => {
    socket.onMessage((raw) => {
      const message = JSON.parse(String(raw)) as { type: string; request_id: string };
      if (message.type === "auth") {
        socket.send(
          JSON.stringify({
            v: 2,
            type: "vault.sessions",
            session_id: "",
            generation: GENERATION,
            request_id: "",
            seq: 1,
            data: { others: [], live: [SESSION] },
          }),
        );
      } else if (message.type === "session.open") {
        const data = snapshotData();
        data.state.live = true;
        const identity = {
          v: 2,
          session_id: SESSION,
          generation: GENERATION,
          request_id: message.request_id,
          seq: 0,
        };
        socket.send(JSON.stringify({ ...identity, type: "session.snapshot", data }));
        socket.send(JSON.stringify({ ...identity, type: "session.synced", data: {} }));
        waiting = () =>
          socket.send(
            JSON.stringify({
              ...identity,
              type: "permission.requested",
              seq: 1,
              native_request: { runtime_id: GENERATION, request_id: PROFILE, turn_id: ROUTINE },
              data: {
                tool_use_id: "synthetic-read",
                kind: "tool",
                tool: "Read",
                summary: "Read synthetic notes",
                question: "Read synthetic notes?",
                reason: "Synthetic permission",
                agent: "",
                plan: "",
              },
            }),
          );
      }
    });
  });
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const vault = `/api/v2/vaults/${workspace.workspace_id}`;
    const bodies: Record<string, unknown> = {
      "/api/rails": {
        agents: [],
        gates: [],
        routines: [],
        counts: { knowledge: 2, sources: 0, inbox: 0 },
        git: {
          state: "unknown",
          ref_state: "",
          branch: "",
          head: "",
          committed_at: "",
          dirty: null,
        },
      },
      "/api/sessions": {
        sessions: [sessionRow({ session_id: SESSION, title: "Synthetic routine chat" })],
        state: "ready",
        reason: "",
        omitted: 0,
      },
      "/api/v2/session-metadata": { sessions: [] },
      "/api/v2/capabilities": capabilitiesFixture({
        workspace: {
          active: {
            id: workspace.workspace_id,
            kind: "vault",
            name: workspace.name,
            policy: workspace.policy,
            layout: "unknown",
            source: "created",
            aliased: false,
            root_available: true,
          },
          chat_only: false,
          selected_id: workspace.workspace_id,
          selected_by: "registration",
          shared_scratch: false,
        },
      }),
      "/api/v2/settings": settingsFixture({
        "onboarding.completed": true,
        "accessibility.font_scale": fontScale,
        "appearance.theme": "dark",
      }),
      "/api/v2/workspaces": {
        ...listingFixture([workspace, routineWorkspace]),
        active_id: workspace.workspace_id,
        selected_id: workspace.workspace_id,
        selected_by: "registration",
        chat_only: false,
      },
      "/api/v2/profiles": { profiles: [profile] },
      [`/api/v2/profiles/${PROFILE}`]: {
        ...profile,
        identity: undefined,
        runtime: undefined,
        provenance: undefined,
        document: profileDocument,
      },
      "/api/v2/routines": {
        routines,
        scheduler: { state: "running", attended: true, paused: false },
      },
      [`/api/v2/routines/${ROUTINE}/runs`]: { runs: [] },
      [`${vault}/entries`]: {
        entries: REFS.map((ref) => ({ ref, title: "Shared title", bytes: 512, modified: 1 })),
        partial: false,
        partial_reasons: [],
        omitted: 0,
      },
      [`${vault}/bookmarks`]: { bookmarks: [] },
      "/api/v2/drafts": { drafts: [], next_after: null },
    };
    let body: unknown = bodies[path];
    let status = body === undefined ? 404 : 200;
    if (path === `${vault}/metadata`) {
      metadataReads += 1;
      if (metadataReads === 1) {
        status = 503;
        body = { error: { code: "unavailable" } };
      } else {
        const { refs } = route.request().postDataJSON() as { refs: string[] };
        body = {
          items: refs.map((ref) => ({
            ref,
            state: "ready",
            reason: null,
            read_bytes: 256,
            display_metadata: metadata,
          })),
          partial: false,
          partial_reasons: [],
          read_bytes: 256 * refs.length,
          omitted: 0,
        };
        status = 200;
      }
    } else if (path.startsWith(`${vault}/notes/`)) {
      const ref = decodeURIComponent(path.slice(`${vault}/notes/`.length));
      noteReads.push(ref);
      body = {
        note: {
          ref,
          title: "Shared title",
          text: "# Shared title\n\nSynthetic note body.",
          source: "# Shared title\n\nSynthetic note body.",
          byte_size: 36,
          sha256: "a".repeat(64),
          frontmatter: {},
          frontmatter_error: null,
          display_metadata: { ...metadata, state: "truncated", heading_state: "truncated" },
        },
        resolved_links: [],
        partial: false,
        partial_reasons: [],
        omitted: 0,
      };
      status = 200;
    } else if (path.startsWith(`${vault}/backlinks/`)) {
      body = { backlinks: [], partial: false, partial_reasons: [], omitted: 0 };
      status = 200;
    } else if (path.startsWith(`${vault}/history/`)) {
      body = { commits: [], state: "unknown" };
      status = 200;
    } else if (path === `/api/v2/routines/${ROUTINE}`) {
      const detail: RoutineDetailData = {
        ...scheduled,
        document: {
          name: scheduled.name,
          purpose: ROUTINE_PURPOSE,
          prompt: "Review synthetic notes.",
          profile_id: PROFILE,
          workspace_id: routineWorkspace.workspace_id,
          deadline_minutes: 10,
          schedule: {
            kind: "daily",
            times: ["09:00"],
            days: [],
            timezone: "Asia/Tokyo",
            start_date: null,
            end_date: null,
            catch_up: "skip",
          },
        },
        tested_revision: 1,
        activates_at: AT,
        next_five: [
          {
            at: NEXT,
            local: "2026-10-13 09:00",
            zone: "Asia/Tokyo",
            offset_minutes: 540,
            note: null,
          },
        ],
        readiness: { blockers: [], run_blockers: [], warnings: ["workspace_not_active"] },
        resolved: {
          profile: {
            profile_id: PROFILE,
            name: PROFILE_NAME,
            revision: 1,
            provider: "claude",
            model: MODEL,
            permission_mode: "plan",
            mode_source: "profile",
            connection_pinned: false,
          },
          workspace: {
            workspace_id: routineWorkspace.workspace_id,
            name: routineWorkspace.name,
            policy: routineWorkspace.policy,
            active: false,
          },
          first_turn: "Review synthetic notes.",
          deadline_minutes: 10,
        },
      };
      body = {
        ...detail,
        next_five: detail.next_five.map((slot) => ({ ...slot, note: slot.note ?? "" })),
      };
      status = 200;
    } else if (path === `/api/v2/routines/${ROUTINE}/pause`) {
      scheduled.state = "paused";
      scheduled.next_run = null;
      scheduled.state_revision += 1;
      body = {
        routine_id: ROUTINE,
        revision: 1,
        state: "paused",
        state_revision: scheduled.state_revision,
      };
      status = 200;
    } else if (path === "/api/e2e/waiting") {
      if (!waiting) throw new Error("Synthetic routine session has not subscribed");
      waiting();
      body = {};
      status = 200;
    }
    return route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body ?? {}),
    });
  });
  // The status bar has no readiness element to poll: register the capabilities
  // response wait before `page.goto` so a reply that lands early is never missed.
  const capabilities = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/v2/capabilities",
  );
  await page.goto(`${baseUrl}#token=minimal-views-synthetic`);
  // Wait for the capabilities response itself, then one animation frame so the render it
  // triggers has painted.
  await capabilities;
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  await page.waitForFunction(
    (scale) => document.documentElement.style.getPropertyValue("--font-scale") === String(scale),
    fontScale,
  );
  await page.evaluate(() => document.fonts.ready);
  return { page, metadataReads: () => metadataReads, noteReads };
}

async function show(page: Page, view: "Vault" | "Bots" | "Routines") {
  await navigateSidebar(page, view);
}

async function expectFit(page: Page, selector: string) {
  const fit = await page.evaluate((selector) => {
    const root = document.querySelector<HTMLElement>(selector);
    if (!root) throw new Error(`Missing view: ${selector}`);
    const bounds = (el: Element) => {
      const box = el.getBoundingClientRect();
      return { left: box.left, right: box.right };
    };
    const nodes = [
      root,
      ...root.querySelectorAll<HTMLElement>(
        "button,input,select,textarea,summary,small,p,span,h1,h2,h3,dt,dd,pre",
      ),
    ].filter((el) => el.getClientRects().length > 0);
    return {
      width: innerWidth,
      root: bounds(root),
      scroll: root.scrollWidth,
      client: root.clientWidth,
      nodes: nodes.map((el) => {
        const range = document.createRange();
        range.selectNodeContents(el);
        return {
          label: el.getAttribute("aria-label") ?? el.textContent,
          box: bounds(el),
          text: [...range.getClientRects()].map((box) => ({ left: box.left, right: box.right })),
        };
      }),
      pageScroll: document.querySelector<HTMLElement>(".app-view")?.scrollLeft,
    };
  }, selector);
  expect(fit.nodes.length).toBeGreaterThan(1);
  expect(fit.root.left).toBeGreaterThanOrEqual(-1);
  expect(fit.root.right).toBeLessThanOrEqual(fit.width + 1);
  const overflowing = fit.nodes
    .slice(1)
    .filter((node) =>
      [node.box, ...node.text].some(
        (box) => box.left < fit.root.left - 1 || box.right > fit.root.right + 1,
      ),
    )
    .slice(0, 4)
    .map((node) => ({
      label: node.label?.trim().slice(0, 100),
      box: node.box,
      text: node.text
        .filter((box) => box.left < fit.root.left - 1 || box.right > fit.root.right + 1)
        .slice(0, 2),
    }));
  expect(fit.scroll, JSON.stringify({ selector, root: fit.root, overflowing })).toBeLessThanOrEqual(
    fit.client + 1,
  );
  expect(fit.pageScroll).toBe(0);
  for (const node of fit.nodes) {
    for (const box of [node.box, ...node.text]) {
      expect(box.left, node.label ?? selector).toBeGreaterThanOrEqual(fit.root.left - 1);
      expect(box.right, node.label ?? selector).toBeLessThanOrEqual(fit.root.right + 1);
    }
  }
}

test.each([
  [400, 1],
  [1440, 1],
  [400, 1.5],
  [1440, 1.5],
])("Vault, Bot settings and Routines fit %i px at font scale %s", async (width, fontScale) => {
  const fixture = await open(width, fontScale);
  const { page } = fixture;
  try {
    await show(page, "Vault");
    await page.locator(".vault-row").first().waitFor();
    expect(await page.locator(".vault-list .vault-ref").allTextContents()).toEqual(REFS);
    expect(await page.getByRole("button", { name: "Load details for this page" }).count()).toBe(0);
    expect(fixture.metadataReads()).toBe(0);
    expect(fixture.noteReads).toEqual([]);
    await expectFit(page, ".vault-browser");
    await page.locator(".vault-row-label > button").first().click();
    const content = page.getByRole("region", { name: "Note content", exact: true });
    await content.waitFor();
    expect(await content.textContent()).toContain("Synthetic note body.");
    expect(await page.getByRole("region", { name: "Note metadata" }).count()).toBe(0);
    // The Vault's own overview line is a second .vault-quiet: the fixture serves no overview
    // (4f673681), so an unqualified locator finds two elements.
    expect(
      await page
        .locator(".vault-quiet", { hasText: "Some details in this note's header" })
        .isVisible(),
    ).toBe(true);
    // The reader's h2, chips and ref sit in their own .vault-reader-head wrapper (4f673681), so
    // .vault-ref is a grandchild of .vault-inspector, not a direct child; the backlinks/related
    // lists elsewhere in .vault-inspector render their own .vault-ref too.
    expect(await page.locator(".vault-reader-head > .vault-ref").textContent()).toBe(REFS[0]);
    expect(fixture.noteReads).toEqual([REFS[0]]);
    expect(fixture.metadataReads()).toBe(0);
    await expectFit(page, ".vault-browser");

    await show(page, "Bots");
    await page.getByRole("button", { name: "Bot settings", exact: true }).click();
    const profileRow = page.getByRole("button", {
      name: `${PROFILE_NAME} — claude — revision 1`,
      exact: true,
    });
    await profileRow.waitFor();
    expect(await profileRow.locator(".profile-summary-purpose").textContent()).toBe(PURPOSE);
    await expectFit(page, ".profiles-view");
    await profileRow.click();
    await page.getByRole("group", { name: "Identity", exact: true }).waitFor();
    expect(await profileRow.getAttribute("aria-current")).toBe("true");
    const profileOverview = page.getByRole("region", { name: "Profile overview", exact: true });
    expect(await profileOverview.locator(".profile-configuration-label").textContent()).toBe(
      "Saved configuration",
    );
    expect(await profileOverview.getByRole("heading").textContent()).toBe(PROFILE_NAME);
    expect(await profileOverview.locator(".profile-overview-identity p").last().textContent()).toBe(
      PURPOSE,
    );
    expect(await profileOverview.locator("dt").allTextContents()).toEqual([
      "Runs on",
      "Model",
      "Permission setting",
    ]);
    expect(await profileOverview.locator("dd").allTextContents()).toEqual([
      "Claude Code",
      MODEL,
      "Plan first",
    ]);
    expect(await profileOverview.locator(".profile-overview-identity p").last().textContent()).toBe(
      PURPOSE,
    );
    if (width === 400 && fontScale === 1.5) {
      // Catalog selection above may scroll the page; verify each selected-profile control when reached.
      await page.setViewportSize({ width: 400, height: 700 });
      const assertVisibleInPane = async (locator: Locator, label: string) => {
        await locator.scrollIntoViewIfNeeded();
        const bounds = await locator.evaluate((node) => {
          const pane = document.querySelector<HTMLElement>("main.app-view");
          if (!pane) throw new Error("Missing profile scroll pane");
          const item = node.getBoundingClientRect();
          const clip = pane.getBoundingClientRect();
          return { top: item.top, bottom: item.bottom, clipTop: clip.top, clipBottom: clip.bottom };
        });
        expect(bounds.top, label).toBeGreaterThanOrEqual(bounds.clipTop);
        expect(bounds.bottom, label).toBeLessThanOrEqual(bounds.clipBottom);
      };
      await assertVisibleInPane(page.locator(".page-title"), "Bot settings title");
      const overviewContent = profileOverview.locator("h1, h2, h3, h4, h5, h6, p, dt, dd");
      for (let index = 0; index < (await overviewContent.count()); index += 1) {
        await assertVisibleInPane(
          overviewContent.nth(index),
          `Profile overview content ${index + 1}`,
        );
      }
      const overviewButtons = profileOverview.locator("button:visible");
      for (let index = 0; index < (await overviewButtons.count()); index += 1) {
        await assertVisibleInPane(
          overviewButtons.nth(index),
          `Profile overview button ${index + 1}`,
        );
      }
      await assertVisibleInPane(page.locator(".bot-look"), "Look controls and status");
      const chooseButton = page.getByRole("button", { name: "Choose icon…", exact: true });
      await assertVisibleInPane(chooseButton, "Choose icon");
      await assertVisibleInPane(
        page.getByRole("button", { name: "Upload image…", exact: true }),
        "Upload image",
      );
      const resetButton = page.getByRole("button", { name: "Reset", exact: true });
      await assertVisibleInPane(resetButton, "Reset image");
      await assertVisibleInPane(chooseButton, "Choose icon opener");
      await chooseButton.click();
      const dialog = page.locator(".bot-icon-dialog");
      const geometry = await dialog.evaluate((node) => {
        const rect = node.getBoundingClientRect();
        const options = node.querySelector<HTMLElement>(".bot-icon-dialog-options");
        const tile = node.querySelector<HTMLElement>(".bot-icon-dialog-option");
        const actions = node.querySelector<HTMLElement>(".bot-icon-dialog-actions");
        if (!options || !tile || !actions) throw new Error("Icon gallery is incomplete");
        const optionsRect = options.getBoundingClientRect();
        const tileRect = tile.getBoundingClientRect();
        const actionsRect = actions.getBoundingClientRect();
        return {
          dialog: { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom },
          options: {
            top: optionsRect.top,
            bottom: optionsRect.bottom,
            scrollTop: options.scrollTop,
          },
          tile: { top: tileRect.top, bottom: tileRect.bottom },
          actions: { top: actionsRect.top, bottom: actionsRect.bottom },
          focused: tile.querySelector("input") === document.activeElement,
          viewport: { width: innerWidth, height: innerHeight },
        };
      });
      expect(geometry.dialog.left).toBeGreaterThanOrEqual(0);
      expect(geometry.dialog.right).toBeLessThanOrEqual(geometry.viewport.width);
      expect(geometry.dialog.top).toBeGreaterThanOrEqual(0);
      expect(geometry.dialog.bottom).toBeLessThanOrEqual(geometry.viewport.height);
      expect(geometry.tile.top).toBeGreaterThanOrEqual(geometry.options.top);
      expect(geometry.tile.bottom).toBeLessThanOrEqual(geometry.options.bottom);
      expect(geometry.options.scrollTop).toBe(0);
      expect(geometry.actions.top).toBeGreaterThanOrEqual(geometry.options.top);
      expect(geometry.actions.bottom).toBeLessThanOrEqual(geometry.dialog.bottom);
      expect(geometry.focused).toBe(true);
      await page.getByRole("button", { name: "Cancel", exact: true }).click();
    }
    const profileLayout = await page.locator(".profiles-layout").evaluate((el) => {
      const container = el.closest<HTMLElement>(".profiles-view");
      if (!container) throw new Error("Missing profile query container");
      const box = (selector: string) => {
        const node = el.querySelector(selector);
        if (!node) throw new Error(`Missing profile pane: ${selector}`);
        const { left, right, top, bottom } = node.getBoundingClientRect();
        return { left, right, top, bottom };
      };
      return {
        // The CSS container query uses layout pixels before the app's font-scale zoom.
        width: container.clientWidth,
        catalog: box(".profiles-catalog"),
        detail: box(".profile-detail"),
      };
    });
    if (profileLayout.width >= 760) {
      expect(profileLayout.detail.left).toBeGreaterThanOrEqual(profileLayout.catalog.right);
      expect(Math.abs(profileLayout.detail.top - profileLayout.catalog.top)).toBeLessThanOrEqual(1);
    } else {
      expect(profileLayout.detail.top).toBeGreaterThanOrEqual(profileLayout.catalog.bottom);
      expect(Math.abs(profileLayout.detail.left - profileLayout.catalog.left)).toBeLessThanOrEqual(
        1,
      );
    }
    const profileEditor = page.locator(".profile-editor");
    expect(await profileEditor.getByLabel("Name", { exact: true }).inputValue()).toBe(PROFILE_NAME);
    expect(
      await profileEditor.getByRole("textbox", { name: "Purpose", exact: true }).inputValue(),
    ).toBe(PURPOSE);
    expect(await profileEditor.getByLabel("Model", { exact: true }).inputValue()).toBe(MODEL);
    expect(
      await profileEditor
        .getByRole("combobox", { name: "Permission mode", exact: true })
        .inputValue(),
    ).toBe("plan");
    const instructions = profileEditor.getByRole("textbox", { name: "Instructions", exact: true });
    await instructions.focus();
    expect(await instructions.evaluate((el) => el === document.activeElement)).toBe(true);
    expect(
      await page.getByRole("region", { name: "Try it in a chat", exact: true }).isVisible(),
    ).toBe(true);
    expect(
      await page.getByRole("button", { name: "Save revision", exact: true }).isDisabled(),
    ).toBe(true);
    await expectFit(page, ".profiles-view");
    await page.getByRole("button", { name: "New profile", exact: true }).focus();
    expect(
      await page
        .getByRole("button", { name: "New profile", exact: true })
        .evaluate((el) => el === document.activeElement),
    ).toBe(true);
    await page.emulateMedia({ forcedColors: "active" });
    try {
      const selectedOutline = await profileRow.evaluate((el) => getComputedStyle(el).outline);
      await page.keyboard.press("Tab");
      const focused = await profileRow.evaluate((el) => {
        const style = getComputedStyle(el);
        return {
          active: el === document.activeElement,
          outline: style.outline,
          outlineStyle: style.outlineStyle,
          outlineWidth: Number.parseFloat(style.outlineWidth),
        };
      });
      expect(focused.active).toBe(true);
      expect(await profileRow.getAttribute("aria-current")).toBe("true");
      expect(focused.outlineStyle).not.toBe("none");
      expect(focused.outlineWidth).toBeGreaterThan(0);
      expect(focused.outline).not.toBe(selectedOutline);
    } finally {
      await page.emulateMedia({ forcedColors: "none" });
    }

    await show(page, "Routines");
    await page.locator(".routines-row-name").first().waitFor();
    expect(await page.locator(".routines-group h2").allTextContents()).toEqual([
      "Needs you",
      "Scheduled",
      "Not scheduled",
      "Archived",
    ]);
    expect(await page.locator(".routines-note").first().textContent()).toContain(
      "Routines run only while VIKI Agent is open.",
    );
    const scheduling = page.getByRole("region", { name: "Scheduling", exact: true });
    expect(await scheduling.locator("strong").textContent()).toBe("Scheduler running");
    const schedulingDetails = scheduling.locator("details");
    expect(await schedulingDetails.getAttribute("open")).toBe(null);
    await page.getByRole("button", { name: "New routine", exact: true }).waitFor();
    await page.getByRole("button", { name: "Pause all", exact: true }).waitFor();
    await expectFit(page, ".routines-view");
    const schedulingSummary = schedulingDetails.locator("summary");
    await schedulingSummary.focus();
    await schedulingSummary.press("Enter");
    expect(await schedulingDetails.getAttribute("open")).not.toBe(null);
    expect(await schedulingDetails.locator("p").textContent()).toContain(
      "When you close the last window, pending confirmation requests are denied at once",
    );
    await expectFit(page, ".routines-view");
    await schedulingSummary.press("Enter");
    const routineRow = page.getByRole("button", { name: ROUTINE_NAME, exact: true });
    const scheduledRow = routineRow.locator("..");
    expect(await scheduledRow.locator(".routines-row-time-label").textContent()).toBe(
      "Next · local time",
    );
    expect(await scheduledRow.locator(".routines-row-clock").textContent()).toContain("00:00");
    const clockBox = await scheduledRow.locator(".routines-row-next").boundingBox();
    const nameBox = await routineRow.boundingBox();
    if (!clockBox || !nameBox) throw new Error("Scheduled row lacks a visible time or name");
    expect(clockBox.x + clockBox.width).toBeLessThanOrEqual(nameBox.x);
    await routineRow.click();
    await page.locator(".routine-preview").waitFor();
    expect(await page.locator(".routine-detail-purpose").textContent()).toBe(ROUTINE_PURPOSE);
    const routineFacts = page.locator(".routine-preview-facts");
    expect(await routineFacts.locator("dt").allTextContents()).toEqual([
      "Profile",
      "Workspace",
      "Schedule zone",
      "Time limit",
    ]);
    expect(await routineFacts.textContent()).toContain(PROFILE_NAME);
    expect(await routineFacts.textContent()).toContain(`Model: ${MODEL}`);
    expect(await routineFacts.textContent()).toContain("Permission mode: plan");
    expect(await routineFacts.textContent()).toContain(routineWorkspace.name);
    expect(await routineFacts.textContent()).toContain("Asia/Tokyo");
    expect(await routineFacts.textContent()).toContain("Stops after at most 10 minutes");
    const prompt = page.locator(".routine-preview-prompt");
    expect(await prompt.getAttribute("open")).toBe(null);
    expect(await prompt.locator("pre").isVisible()).toBe(false);
    const routineWarning = page
      .locator(".routine-preview")
      .getByText(INACTIVE_WARNING, { exact: true });
    expect(await routineWarning.isVisible()).toBe(true);
    expect(await routineWarning.evaluate((el) => el.closest("details"))).toBe(null);
    expect(await page.locator(".routine-preview-slots").isVisible()).toBe(true);
    expect(
      await page.locator(".routine-preview-slots").evaluate((el) => el.closest("details")),
    ).toBe(null);
    expect(await page.locator(".routine-preview-note").isVisible()).toBe(true);
    expect(
      await page
        .locator(".routine-detail-actions")
        .getByRole("button", { name: "Edit", exact: true })
        .isVisible(),
    ).toBe(true);
    expect(await page.locator(".routine-preview").textContent()).toContain(
      "2026-10-13 09:00 Asia/Tokyo",
    );
    expect(await routineRow.getAttribute("aria-current")).toBe("true");
    const promptSummary = prompt.locator("summary");
    await promptSummary.focus();
    await promptSummary.press("Enter");
    expect(await prompt.locator("pre").isVisible()).toBe(true);
    expect(await prompt.locator("pre").textContent()).toBe("Review synthetic notes.");
    await expectFit(page, ".routines-view");
    await promptSummary.press("Enter");
    await routineRow.focus();
    await page.evaluate(() =>
      fetch("/api/e2e/waiting", { method: "POST" }).then((response) => response.json()),
    );
    await page.waitForFunction((name) => {
      const row = [...document.querySelectorAll(".routines-row")].find(
        (el) => el.querySelector("button")?.textContent === name,
      );
      return row?.classList.contains("routines-row--attention");
    }, ROUTINE_NAME);
    expect(await routineRow.evaluate((el) => el === document.activeElement)).toBe(true);
    expect(
      await routineRow.evaluate((el) => {
        let previous = el.closest("li")?.previousElementSibling;
        while (previous && !previous.classList.contains("routines-group"))
          previous = previous.previousElementSibling;
        return previous?.textContent;
      }),
    ).toBe("Needs you");
    expect(await page.locator(".routine-detail-run").textContent()).toContain(
      "Waiting for your confirmation.",
    );
    expect(
      await page
        .locator(".routine-detail-run")
        .getByRole("button", { name: "Open session", exact: true })
        .isVisible(),
    ).toBe(true);
    expect(
      await page
        .locator(".routine-detail-run")
        .getByRole("button", { name: "Stop", exact: true })
        .isVisible(),
    ).toBe(true);
    expect(await routineWarning.isVisible()).toBe(true);
    await page
      .locator(".routine-detail-actions")
      .getByRole("button", { name: "Pause", exact: true })
      .click();
    await page.waitForFunction(
      (name) =>
        [...document.querySelectorAll(".routines-row")].some(
          (el) =>
            el.querySelector("button")?.textContent === name &&
            el.textContent?.includes("Paused") &&
            el.classList.contains("routines-row--attention"),
        ),
      ROUTINE_NAME,
    );
    expect(await page.locator(".routine-detail-run").textContent()).toContain(
      "Waiting for your confirmation.",
    );
    expect(await routineWarning.isVisible()).toBe(true);
    await expectFit(page, ".routines-view");
  } finally {
    await page.close();
  }
});
