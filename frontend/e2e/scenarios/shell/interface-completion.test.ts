/** Actual App over synthetic REST/socket fixtures; no provider, vault or screenshots. */
import type { Browser, Locator, Page } from "playwright-core";
import { createServer, type ViteDevServer } from "vite";
import { afterAll, beforeAll, expect, test } from "vitest";
import { GENERATION, sessionRow, snapshotData } from "../../../src/accept/m3b/fakes";
import type { ToolInventory } from "../../../src/clients/conversations/inventoryApi";
import type { SessionMetadata } from "../../../src/clients/conversations/sessionMetadataApi";
import type { NativeCatalog } from "../../../src/clients/profiles/nativeAssetsApi";
import {
  blankProfileDocument,
  type ProfileDefinition,
} from "../../../src/clients/profiles/profileApi";
import type { PreparedProfilePreview } from "../../../src/clients/profiles/profileLaunchApi";
import { serializeLayout } from "../../../src/state/paneFormat";
import { focusGroup, moveTab, openTab, singlePane } from "../../../src/state/panes";
import { PANES_STORAGE_KEY } from "../../../src/state/usePanes";
import {
  capabilitiesFixture,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../../src/testShell";
import {
  ACCEPTED_MEDIA_TYPES,
  MAX_ATTACHMENTS,
  MAX_FILE_BYTES,
  MAX_TURN_BYTES,
} from "../../../src/turnInput";
import { launchEdge, waitForCount } from "../../harness";
import { navigateSidebar } from "../../sidebar-navigation";

const PORT = 5424;
const ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const PROFILE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const WORKSPACE = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const NOW = "2026-10-12T00:00:00.000Z";
let browser: Browser | undefined;
let server: ViteDevServer | undefined;

beforeAll(async () => {
  server = await createServer({
    server: { host: "127.0.0.1", port: PORT, strictPort: true },
    logLevel: "silent",
  });
  await server.listen();
  browser = await launchEdge();
});
afterAll(async () => {
  await browser?.close();
  await server?.close();
});

function profile(): ProfileDefinition {
  const document = blankProfileDocument();
  document.identity.name = "Synthetic profile";
  document.tools.mcp_configuration = {
    servers: [
      {
        name: "Library",
        transport: "stdio",
        command: "synthetic",
        args: [],
        url: null,
        note: null,
      },
      {
        name: "Calendar",
        transport: "http",
        command: null,
        args: [],
        url: "https://example.invalid/mcp",
        note: null,
      },
    ],
  };
  return {
    profile_id: PROFILE,
    revision: 3,
    current_revision: 3,
    state_revision: 1,
    archived: false,
    document,
    created_at: NOW,
    updated_at: NOW,
  };
}

const catalog: NativeCatalog = {
  source_id: "workspace:synthetic",
  source_scope: "workspace",
  provider: "claude",
  assets: ["outline", "trace"].map((name) => ({
    source_id: "workspace:synthetic",
    source_scope: "workspace",
    provider: "claude",
    relative_id: `skills/${name}/SKILL.md`,
    kind: "skill",
    name: "Same skill",
    description: name === "trace" ? "Trace a synthetic issue" : "Outline a synthetic idea",
    model: null,
    tools: [],
    digest: "a".repeat(64),
    observed_at: NOW,
    supported_fields: ["name", "description"],
    unsupported_fields: [],
    launch_supported: false,
  })),
  partial: true,
  partial_reasons: ["entry_limit"],
  scanned_entries: 2,
  scanned_bytes: 256,
  omitted: 1,
};

const inventory: ToolInventory = {
  observed_at: NOW,
  system_tools: ["Read"],
  mcp_tools: null,
  mcp_servers: [
    {
      name: "actual-plugin",
      status: "disabled",
      source: "plugin",
      scope: "project",
      server_name: null,
      server_version: null,
      egress: null,
      tools: [],
      tools_omitted: false,
    },
    {
      name: "plugin-looking",
      status: "connected",
      source: null,
      scope: "dynamic",
      server_name: null,
      server_version: null,
      egress: null,
      tools: [],
      tools_omitted: false,
    },
  ],
  skills: [],
  memory_files: [],
  agents: [],
  reasons: { mcp_tools: "unrecognized" },
};

type RequestRecord = { path: string; method: string; body: Record<string, unknown> | null };
type Options = {
  width?: number;
  height?: number;
  scale?: number;
  live?: boolean;
  target?: "refused" | "invalid";
  split?: boolean;
  draft?: string;
  blocked?: boolean;
  workspace?: boolean;
};

async function open(options: Options = {}) {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({
    viewport: { width: options.width ?? 1440, height: options.height ?? 900 },
  });
  const requests: RequestRecord[] = [];
  const frames: Record<string, unknown>[] = [];
  const labels = new Map<string, SessionMetadata>(
    [ID, OTHER].map((id) => [
      id,
      {
        session_id: id,
        title: id === ID ? "Design session" : "Second session",
        revision: 4,
        pinned: false,
        archived: false,
        unread: false,
        color: null,
      },
    ]),
  );
  const drafts = new Map<string, Record<string, unknown>>();
  let prepared: PreparedProfilePreview | undefined;
  // A two-group layout, ID's own pane focused/selected, OTHER alone in the second pane — a
  // main pane plus a side pane.
  const splitPanes = serializeLayout(
    focusGroup(moveTab(openTab(singlePane(), ID), OTHER, { group: "g1", zone: "right" }), "g1"),
  );
  await page.addInitScript(
    ({ split, id, panesKey, panes }) => {
      if (split) {
        sessionStorage.setItem("viki.selected", id);
        sessionStorage.setItem(panesKey, panes);
      }
    },
    { split: options.split, id: ID, panesKey: PANES_STORAGE_KEY, panes: splitPanes },
  );
  await page.routeWebSocket("**/ws", (socket) => {
    socket.onMessage((raw) => {
      const message = JSON.parse(String(raw)) as Record<string, unknown>;
      frames.push(message);
      if (message.type === "auth" && options.live) {
        socket.send(
          JSON.stringify({
            v: 2,
            type: "vault.sessions",
            session_id: "",
            generation: "",
            request_id: "",
            seq: 1,
            data: { live: [ID], others: [] },
          }),
        );
      }
      if (message.type === "history.more") {
        socket.send(
          JSON.stringify({
            v: 2,
            type: "history.page",
            session_id: message.session_id,
            generation: message.generation,
            request_id: message.request_id,
            data: { events: [], next_before_uuid: null },
          }),
        );
        return;
      }
      if (message.type !== "session.open") return;
      const identity = {
        v: 2,
        session_id: message.session_id,
        generation: GENERATION,
        request_id: message.request_id,
        seq: 1,
      };
      if (options.target === "refused") {
        socket.send(
          JSON.stringify({
            ...identity,
            type: "session.open.refused",
            generation: "",
            seq: 0,
            data: { reason: "unavailable" },
          }),
        );
        return;
      }
      const data = snapshotData();
      data.state.live = options.live === true && message.session_id === ID;
      socket.send(JSON.stringify({ ...identity, type: "session.snapshot", data }));
      socket.send(JSON.stringify({ ...identity, type: "session.synced", data: {} }));
    });
  });
  await page.route("**/api/**", (route) => {
    const request = route.request(),
      url = new URL(request.url()),
      path = url.pathname;
    const input = request.postData() ? (request.postDataJSON() as Record<string, unknown>) : null;
    requests.push({ path, method: request.method(), body: input });
    if (path.startsWith("/api/v2/session-metadata/") && request.method() === "PATCH") {
      const id = path.split("/").at(-1) ?? "";
      const row = labels.get(id);
      if (!row) return route.fulfill({ status: 404, json: { error: { code: "not_found" } } });
      const next = { ...row, ...(input?.patch as object), revision: row.revision + 1 };
      labels.set(id, next);
      return route.fulfill({ json: next });
    }
    if (path === "/api/v2/profiles" && request.method() === "POST")
      return route.fulfill({
        json: { profile_id: input?.profile_id, revision: 1, state_revision: 1 },
      });
    if (path.endsWith("/start-preview") && input) {
      const profileId = path.split("/")[4] ?? "";
      if (options.blocked)
        return route.fulfill({
          status: 422,
          json: {
            launchable: false,
            profile_id: profileId,
            profile_revision: input.expected_profile_revision,
            fields: [
              {
                field: "memory.mode",
                status: "unsupported",
                preview_class: "unsupported",
                effective_value: "isolated",
                source: "profile",
                reason: "isolation_unproven",
              },
            ],
            blockers: ["memory.mode"],
          },
        });
      prepared = {
        launchable: true,
        fields: [],
        blockers: [],
        warnings: [],
        prefs: { mode: "default", effort: "high", model: null },
        session_id: String(input.session_id),
        profile_id: profileId,
        profile_revision: Number(input.expected_profile_revision),
        generation: GENERATION,
        workspace_id: String(input.workspace_id),
        workspace_revision: 1,
        policy: "ordinary",
        policy_revision: 1,
        settings_revision: 1,
        runtime: "claude",
        capability_key: "a".repeat(64),
        draft: {
          key: String(input.session_id),
          revision: 1,
          text: "",
          saved_at: null,
          attachments: [],
        },
        started: false,
        identity: {
          name: "Prepared identity",
          avatar_asset_id: null,
          accent: "orange",
          shape: "rounded",
          profile_revision: Number(input.expected_profile_revision),
        },
      };
      return route.fulfill({ json: prepared });
    }
    if (prepared && path === `/api/v2/profile-sessions/${prepared.session_id}`)
      return route.fulfill({ json: prepared });
    // The restored ordinary chats have no profile binding.
    if ([ID, OTHER].some((id) => path === `/api/v2/profile-sessions/${id}`))
      return route.fulfill({ status: 404, json: { error: { code: "not_found" } } });
    const draftKey = /^\/api\/v2\/drafts\/([^/]+)$/.exec(path)?.[1];
    if (draftKey) {
      const previous = drafts.get(draftKey) ?? {
        key: draftKey,
        revision: options.draft ? 1 : 0,
        text: options.draft ?? "",
        saved_at: options.draft ? NOW : null,
      };
      const next =
        request.method() === "PUT"
          ? { ...previous, ...input, revision: Number(input?.expected_revision) + 1, saved_at: NOW }
          : previous;
      drafts.set(draftKey, next);
      return route.fulfill({ json: next });
    }
    const source = profile();
    const bodies: Record<string, unknown> = {
      "/api/sessions": {
        sessions: [ID, OTHER].map((id) =>
          sessionRow({ session_id: id, title: labels.get(id)?.title ?? "" }),
        ),
        state: "ready",
        reason: "",
        omitted: 0,
      },
      "/api/rails": {
        agents: [],
        gates: [],
        routines: [],
        counts: { knowledge: 0, sources: 0, inbox: 0 },
        git: {
          state: "known",
          ref_state: "branch",
          branch: "test",
          head: "1234567",
          committed_at: "",
          dirty: 0,
        },
      },
      "/api/v2/attachment-capabilities": {
        provider: "claude",
        media_types: ACCEPTED_MEDIA_TYPES,
        max_files: MAX_ATTACHMENTS,
        max_file_bytes: MAX_FILE_BYTES,
        max_turn_bytes: MAX_TURN_BYTES,
        max_dimension: 8192,
      },
      "/api/v2/capabilities": options.workspace
        ? capabilitiesFixture({
            workspace: {
              active: {
                id: WORKSPACE,
                kind: "vault",
                name: "Synthetic workspace",
                policy: "ordinary",
                layout: "unknown",
                source: "created",
                aliased: false,
                root_available: true,
              },
              chat_only: false,
              shared_scratch: false,
            },
          })
        : capabilitiesFixture(),
      "/api/v2/settings": settingsFixture({
        "onboarding.completed": true,
        "appearance.theme": "dark",
        "accessibility.font_scale": options.scale ?? 1,
      }),
      "/api/v2/workspaces": options.workspace
        ? {
            ...listingFixture([
              workspaceFixture({ workspace_id: WORKSPACE, name: "Synthetic workspace" }),
            ]),
            active_id: WORKSPACE,
            chat_only: false,
          }
        : listingFixture(),
      "/api/v2/session-metadata": { sessions: [...labels.values()] },
      "/api/v2/drafts": { drafts: [], next_after: null },
      "/api/v2/routines": {
        routines: [],
        scheduler: { state: "running", attended: true, paused: false },
      },
      "/api/v2/native-assets": url.searchParams.has("source_id")
        ? catalog
        : { sources: [{ source_id: catalog.source_id, provider: "claude", scope: "workspace" }] },
      [`/api/v2/sessions/${ID}/inventory`]: inventory,
      "/api/v2/profiles": {
        profiles: [
          {
            profile_id: source.profile_id,
            revision: source.revision,
            current_revision: source.current_revision,
            state_revision: source.state_revision,
            archived: source.archived,
            created_at: NOW,
            updated_at: NOW,
            identity: source.document.identity,
            runtime: source.document.runtime,
            provenance: source.document.provenance,
          },
        ],
      },
      [`/api/v2/profiles/${PROFILE}`]: source,
      "/api/v2/session-windows": { opened: true },
    };
    return route.fulfill({
      status: bodies[path] === undefined ? 404 : 200,
      json: bodies[path] ?? {},
    });
  });
  const target = options.target ? `&session=${options.target === "invalid" ? "invalid" : ID}` : "";
  await page.goto(`http://127.0.0.1:${PORT}/#token=interface-completion-synthetic${target}`);
  await page.locator("[data-region=topbar]").waitFor();
  await page.waitForFunction(
    (wanted) =>
      document.documentElement.style.getPropertyValue("--font-scale") === wanted &&
      getComputedStyle(document.documentElement).zoom === wanted,
    String(options.scale ?? 1),
  );
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise<void>((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
    );
  });
  return { page, requests, frames, labels };
}

async function primary(page: Page, value: "chat" | "capabilities") {
  await navigateSidebar(page, value === "chat" ? "Chat" : "Capabilities");
}
async function rail(page: Page) {
  const toggle = page.getByRole("button", { name: "Sessions and agents", exact: true });
  if ((await toggle.isVisible()) && (await toggle.getAttribute("aria-expanded")) !== "true")
    await toggle.click();
  return page.locator(`[data-session-id="${ID}"]`);
}
async function fits(locator: Locator) {
  return locator.evaluate((node) => {
    const box = node.getBoundingClientRect();
    return {
      fits:
        box.left >= -1 && box.right <= innerWidth + 1 && node.scrollWidth <= node.clientWidth + 1,
      vertical: box.top >= -1 && box.bottom <= innerHeight + 1,
      box: box.toJSON(),
      scrollWidth: node.scrollWidth,
      clientWidth: node.clientWidth,
    };
  });
}
function writes(requests: RequestRecord[], path: string) {
  return requests.filter((request) => request.path === path && request.method !== "GET");
}

test.each([
  [1440, 900, 1],
  [400, 700, 1],
  [400, 700, 1.5],
  [320, 600, 1.5],
])("five-tab hub retains navigation and fits %s×%s at scale %s", async (width, height, scale) => {
  const { page } = await open({ width, height, scale });
  try {
    await primary(page, "capabilities");
    const topbar = page.locator("[data-region=topbar]");
    expect(await topbar.getByRole("tablist", { name: "Primary", exact: true }).count()).toBe(0);
    expect(await topbar.getByRole("combobox", { name: "Primary view" }).count()).toBe(0);
    expect(await topbar.getByRole("button", { name: "New session", exact: true }).count()).toBe(0);
    const hub = page.getByRole("region", { name: "Capabilities", exact: true });
    await hub.getByRole("list", { name: "Returned native skills" }).waitFor();
    expect(await hub.getByRole("tab").allTextContents()).toEqual([
      "Skills",
      "Subagents",
      "Tools",
      "Connectors",
      "Plugins",
    ]);
    for (const locator of [
      page.locator("[data-region=topbar]"),
      hub,
      hub.locator(".capabilities-tabs"),
    ]) {
      const probe = await fits(locator);
      expect(probe.fits, JSON.stringify(probe)).toBe(true);
    }
    await hub.getByRole("tab", { name: "Skills" }).focus();
    await page.keyboard.press("End");
    expect(await hub.getByRole("tab", { name: "Plugins" }).getAttribute("aria-selected")).toBe(
      "true",
    );
    await page.keyboard.press("Home");
    expect(await hub.getByRole("tab", { name: "Skills" }).getAttribute("aria-selected")).toBe(
      "true",
    );
    const row = await rail(page);
    await row.waitFor();
    const sidebar = page.getByRole("navigation", { name: "Navigation and sessions" });
    expect(await sidebar.locator(".rail-brand, .rail-monogram").count()).toBe(0);
    expect(
      await sidebar.getByRole("button", { name: "New session", exact: true }).isVisible(),
    ).toBe(true);
    if (width < 800) {
      expect(await page.getByRole("dialog", { name: "Sessions and agents" }).isVisible()).toBe(
        true,
      );
      await page.keyboard.press("Escape");
    } else
      expect(
        await page.getByRole("navigation", { name: "Navigation and sessions" }).isVisible(),
      ).toBe(true);
    await primary(page, "chat");
    await page.getByText("New conversation", { exact: true }).waitFor();
  } finally {
    await page.close();
  }
});

test("skills, tools and plugins expose exact evidence and search only returned rows", async () => {
  const { page, requests } = await open({ live: true });
  try {
    await primary(page, "capabilities");
    const hub = page.getByRole("region", { name: "Capabilities", exact: true });
    const skills = hub.getByRole("list", { name: "Returned native skills" });
    await skills.waitFor();
    expect(await skills.getByRole("button").count()).toBe(2);
    const readsBeforeSearch = requests.filter(
      (request) => request.path === "/api/v2/native-assets",
    ).length;
    await hub.getByRole("searchbox", { name: "Search native skills" }).fill("trace");
    await skills.getByRole("button").click();
    const detail = hub.getByRole("region", { name: "Selected skill" });
    expect(await detail.textContent()).toContain("Ref: skills/trace/SKILL.md");
    expect(await detail.textContent()).toContain("Source: workspace:synthetic");
    expect(await hub.getByText("Catalog coverage: partial").isVisible()).toBe(true);
    await hub.getByText("Catalog read details", { exact: true }).click();
    expect(await hub.getByText("Omitted catalog entries: 1").isVisible()).toBe(true);
    expect(requests.filter((request) => request.path === "/api/v2/native-assets")).toHaveLength(
      readsBeforeSearch,
    );
    await hub.getByRole("tab", { name: "Tools", exact: true }).click();
    await hub.getByRole("combobox", { name: "Running chat" }).selectOption(ID);
    await hub.getByRole("button", { name: /^Read/ }).waitFor();
    expect(await hub.getByText(/Partial observation/).isVisible()).toBe(true);
    await hub.getByRole("searchbox", { name: "Search tools" }).fill("missing");
    expect(await hub.getByText("No matching tools.").isVisible()).toBe(true);
    await hub.getByRole("tab", { name: "Plugins", exact: true }).click();
    await hub.getByRole("combobox", { name: "Running chat" }).selectOption(ID);
    await hub.getByRole("button", { name: /^actual-plugin/ }).waitFor();
    expect(await hub.getByRole("button", { name: /^plugin-looking/ }).count()).toBe(0);
    expect(await hub.getByRole("article", { name: "actual-plugin" }).textContent()).toContain(
      "disabled",
    );
    expect(await hub.getByText(/plugin membership is unknown/).isVisible()).toBe(true);
  } finally {
    await page.close();
  }
});

test("connector subset survives tab switches and canceled navigation before reviewed copy preparation", async () => {
  const { page, requests, frames } = await open({ workspace: true });
  try {
    await primary(page, "capabilities");
    const hub = page.getByRole("region", { name: "Capabilities", exact: true });
    await hub.getByRole("tab", { name: "Connectors", exact: true }).click();
    await hub.getByRole("combobox", { name: "Saved app profile" }).selectOption(`${PROFILE}:3`);
    const calendar = hub.getByRole("checkbox", { name: "Include Calendar in next session" });
    await calendar.uncheck();
    await hub.getByRole("tab", { name: "Tools", exact: true }).click();
    await hub.getByRole("tab", { name: "Connectors", exact: true }).click();
    expect(await calendar.isChecked()).toBe(false);
    page.once("dialog", (dialog) => void dialog.dismiss());
    await hub.getByRole("button", { name: "Manage profiles" }).click();
    expect(await hub.isVisible()).toBe(true);
    await hub.getByRole("button", { name: "Review next session" }).click();
    expect(writes(requests, "/api/v2/profiles")).toHaveLength(0);
    const review = hub.getByRole("region", { name: "Next session review" });
    expect(await review.textContent()).toContain("Library");
    expect(await review.textContent()).not.toContain("Calendar");
    await review.getByRole("button", { name: "Save new profile copy and prepare draft" }).click();
    await page
      .locator("[data-region=conversation-header]")
      .getByText(/Prepared identity/)
      .waitFor();
    const copies = writes(requests, "/api/v2/profiles");
    expect(copies).toHaveLength(1);
    expect(copies[0]?.body?.profile_id).not.toBe(PROFILE);
    const document = copies[0]?.body?.document as ReturnType<typeof blankProfileDocument>;
    expect(document.tools.mcp_configuration).toMatchObject({ servers: [{ name: "Library" }] });
    expect(document.runtime).toEqual(profile().document.runtime);
    expect(writes(requests, `/api/v2/profiles/${PROFILE}`)).toHaveLength(0);
    expect(frames.some((frame) => frame.type === "turn.send")).toBe(false);
    expect(
      requests.some((request) => request.path === "/api/v2/commands" && request.method === "POST"),
    ).toBe(false);
  } finally {
    await page.close();
  }
});

test("authoritative blocked connector review keeps selection and does not open a conversation", async () => {
  const { page, frames } = await open({ blocked: true, workspace: true });
  try {
    await primary(page, "capabilities");
    const hub = page.getByRole("region", { name: "Capabilities", exact: true });
    await hub.getByRole("tab", { name: "Connectors", exact: true }).click();
    await hub.getByRole("combobox", { name: "Saved app profile" }).selectOption(`${PROFILE}:3`);
    await hub.getByRole("button", { name: "Review next session" }).click();
    await hub.getByRole("button", { name: "Prepare draft from saved revision" }).click();
    const preview = hub.getByRole("region", { name: "Next session review" }).getByRole("status");
    await preview.getByText("Draft is blocked.", { exact: true }).waitFor();
    const blocker = preview.getByRole("listitem").filter({ hasText: /isolation_unproven/ });
    const field = preview.getByRole("paragraph").filter({ hasText: /isolation_unproven/ });
    expect(await blocker.count()).toBe(1);
    expect(await field.count()).toBe(1);
    expect(await blocker.isVisible()).toBe(true);
    expect(await field.isVisible()).toBe(true);
    expect(await blocker.textContent()).toBe(await field.textContent());
    expect(
      await hub.getByRole("checkbox", { name: "Include Library in next session" }).isChecked(),
    ).toBe(true);
    expect(
      frames.some((frame) => frame.type === "session.open" || frame.type === "turn.send"),
    ).toBe(false);
  } finally {
    await page.close();
  }
});

test("unread and color menus persist CAS labels, restore focus and hand off the same session", async () => {
  const { page, requests, frames } = await open({ width: 400, height: 700, scale: 1.5 });
  try {
    const row = await rail(page),
      trigger = row.getByRole("button", { name: "Session menu" });
    await trigger.focus();
    await page.keyboard.press("ArrowDown");
    await page.getByRole("menuitem", { name: "Mark as unread" }).click();
    await page.waitForFunction(
      (id) =>
        document.querySelector(`[data-session-id="${id}"]`)?.getAttribute("data-unread") === "true",
      ID,
    );
    expect(writes(requests, `/api/v2/session-metadata/${ID}`)[0]?.body).toEqual({
      expected_revision: 4,
      patch: { unread: true },
    });
    await trigger.press("ArrowDown");
    await page.getByRole("menuitem", { name: "Color label" }).focus();
    await page.keyboard.press("ArrowRight");
    const purple = page.getByRole("menuitemradio", { name: "Purple color label" });
    await purple.waitFor();
    const probe = await fits(page.locator(".session-color-menu"));
    expect(probe.fits, JSON.stringify(probe)).toBe(true);
    expect(probe.vertical, JSON.stringify(probe)).toBe(true);
    const hit = await purple.evaluate((node) => {
      const box = node.getBoundingClientRect();
      const target = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
      const drawer = node.closest(".rail-drawer");
      return {
        hit: node.contains(target),
        target: target?.tagName,
        box: box.toJSON(),
        drawer: drawer?.getBoundingClientRect().toJSON(),
        drawerOverflow: drawer ? getComputedStyle(drawer).overflow : null,
        drawerScroll: drawer ? [drawer.scrollLeft, drawer.scrollTop] : null,
      };
    });
    expect(hit.hit, JSON.stringify(hit)).toBe(true);
    // A measured viewport point checks the visible popup under the app's font zoom.
    await page.mouse.click(hit.box.x + hit.box.width / 2, hit.box.y + hit.box.height / 2);
    await page.waitForFunction(
      (id) => document.querySelector(`[data-session-id="${id}"] [data-color="purple"]`) !== null,
      ID,
    );
    expect(writes(requests, `/api/v2/session-metadata/${ID}`)[1]?.body).toEqual({
      expected_revision: 5,
      patch: { color: "purple" },
    });
    expect(await trigger.evaluate((node) => document.activeElement === node)).toBe(true);
    await trigger.press("ArrowDown");
    const windowResponse = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/v2/session-windows",
    );
    await page.getByRole("menuitem", { name: "New window", exact: true }).click();
    await windowResponse;
    expect(writes(requests, "/api/v2/session-windows")[0]?.body).toEqual({ session_id: ID });
    expect(
      frames.some((frame) => frame.type === "session.open" || frame.type === "turn.send"),
    ).toBe(false);
    expect(await row.getAttribute("data-unread")).toBe("true");
  } finally {
    await page.close();
  }
});

test("new chat shows its greeting and draft without starter buttons or an AI notice", async () => {
  const { page, frames, requests } = await open({ draft: "Current draft" });
  try {
    const box = page.getByRole("textbox", { name: "Message", exact: true });
    await page.waitForFunction(
      () =>
        document.querySelector<HTMLTextAreaElement>('[aria-label="Message"]')?.value ===
        "Current draft",
    );
    await page.getByText("New conversation", { exact: true }).waitFor();
    for (const name of ["Explore an idea", "Review a note", "Work on code"]) {
      expect(await page.getByRole("button", { name, exact: true }).count()).toBe(0);
    }
    expect(await page.getByText(/You are talking to an AI assistant/).count()).toBe(0);
    expect(await box.inputValue()).toBe("Current draft");
    expect(
      frames.some((frame) => frame.type === "session.open" || frame.type === "turn.send"),
    ).toBe(false);
    expect(
      requests.some((request) => request.path === "/api/v2/commands" && request.method === "POST"),
    ).toBe(false);
  } finally {
    await page.close();
  }
});

test.each(["refused", "invalid"] as const)(
  "%s launch target remains unavailable without automatic fallback",
  async (target) => {
    const { page, frames } = await open({ target });
    try {
      await page
        .getByRole("heading", {
          name:
            target === "refused"
              ? "This conversation is unavailable."
              : "This conversation link is invalid.",
        })
        .waitFor();
      expect(await page.getByRole("textbox", { name: "Message", exact: true }).count()).toBe(0);
      const opens = frames.filter((frame) => frame.type === "session.open");
      expect(opens).toHaveLength(target === "refused" ? 1 : 0);
      if (target === "refused")
        expect(opens[0]).toMatchObject({ session_id: ID, existing_only: true });
      expect(frames.some((frame) => frame.type === "turn.send")).toBe(false);
      expect(await page.evaluate(() => location.hash)).toBe("");
    } finally {
      await page.close();
    }
  },
);

test("restored split fits the conversation layout without starter buttons", async () => {
  const { page } = await open({ split: true, width: 1440, height: 900 });
  try {
    await waitForCount(page, "section.conversation.pane", 2);
    await page.getByText("New conversation", { exact: true }).first().waitFor();
    expect(await page.getByRole("region", { name: "Conversation starters" }).count()).toBe(0);
    const panes = page.locator("section.conversation.pane");
    for (const index of [0, 1]) {
      const probe = await fits(panes.nth(index));
      expect(probe.fits, JSON.stringify(probe)).toBe(true);
    }
    await page.setViewportSize({ width: 400, height: 700 });
    await waitForCount(page, "section.conversation.pane", 0);
    await (await rail(page)).waitFor();
    await page.keyboard.press("Escape");
    await page.setViewportSize({ width: 1440, height: 900 });
    await waitForCount(page, "section.conversation.pane", 2);
  } finally {
    await page.close();
  }
});
