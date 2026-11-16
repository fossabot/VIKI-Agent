/**
 * Synthetic Vault browser captures against Vite and real Edge. Vault REST requests are answered
 * here; no backend, vault folder or provider is used. The app's WebSocket stays offline.
 *
 * Run from frontend/ with the shot harness after a fresh build:
 *   npx vitest run --config vitest.shots.config.ts e2e/vault.shots.ts
 *
 * PNGs land in `frontend/test-results/vault/`.
 */
// @ts-expect-error the shot harness's node.d.ts only declares its server helpers.
import { mkdirSync } from "node:fs";
import type { Browser, Page } from "playwright-core";
import { createServer, type ViteDevServer } from "vite";
import { afterAll, beforeAll, test } from "vitest";
import type { ModelConnectionsListing } from "../src/clients/app/modelConnectionsApi";
import type {
  Capabilities,
  FolderPickerStatus,
  SettingsView,
  WorkspacesListing,
} from "../src/clients/app/shellApi";
import type { SchedulerStatus } from "../src/clients/automation/routinesApi";
import type { ArchiveSummary } from "../src/clients/conversations/chatArchivesApi";
import type { Draft, DraftPage } from "../src/clients/conversations/durableApi";
import type { PlanLimitReading } from "../src/clients/conversations/limitsApi";
import type { SessionMetadata } from "../src/clients/conversations/sessionMetadataApi";
import type { ChatPrefs } from "../src/clients/conversations/sessionPrefsApi";
import type { BotsList } from "../src/clients/messaging/botsApi";
import type { NativeCatalog, NativeSource } from "../src/clients/profiles/nativeAssetsApi";
import type { ProviderModelView } from "../src/clients/profiles/profileLaunchApi";
import type { CaptureListing } from "../src/clients/vaults/capturesApi";
import type { Rails, SessionsResult } from "../src/protocol";
import type { DecisionInbox } from "../src/state/decisionsApi";
import type {
  OverviewNote,
  VaultBookmark,
  VaultEntry,
  VaultNote,
  VaultNoteHistory,
  VaultOverview,
} from "../src/state/vaultApi";
import type { MemoryListing } from "../src/state/vaultKnowledgeApi";
import type { RecoveryList } from "../src/state/vaultOpsApi";
import {
  ACCEPTED_MEDIA_TYPES,
  MAX_ATTACHMENTS,
  MAX_FILE_BYTES,
  MAX_TURN_BYTES,
} from "../src/turnInput";
import type { VoiceStatus } from "../src/voice/dictationTypes";
import { launchEdge } from "./harness";
import { navigateSidebar } from "./sidebar-navigation";

const PORT = 5197;
const ORIGIN = `http://127.0.0.1:${PORT}`;
const OUT = "test-results/vault";
const WORKSPACE = "22222222-2222-4222-8222-222222222222";
const DAY = 24 * 60 * 60 * 1000;
const today = new Date();
const date = (daysAgo: number): string =>
  new Date(today.getTime() - daysAgo * DAY).toISOString().slice(0, 10);
const stamp = (daysAgo: number): string => new Date(today.getTime() - daysAgo * DAY).toISOString();

const entries: readonly [string, string, number][] = [
  ["00-inbox/capture.md", "Inbox capture", 1],
  ["30-knowledge/summary.md", "A useful summary", 0],
  ["30-knowledge/old-knowledge.md", "Old Knowledge", 45],
  ["50-maps/research-map.md", "Research map", 3],
  ["90-archive/legacy.md", "Legacy folder note", 6],
  ["unknown folder/field-note.md", "Field note", 8],
  ["30-knowledge/unread.md", "Unread listing-only note", 2],
] as const;
const listing: VaultEntry[] = entries.map(([ref, title, age]) => ({
  ref,
  title,
  modified: Math.floor(today.getTime() / 1000) - age * 24 * 60 * 60,
  bytes: 480,
}));
const overviewRow = (
  ref: string,
  title: string,
  summary: string | null,
  age: number,
  tags: string[] | null = [],
  type: string | null = null,
  links: number | null = 0,
): OverviewNote => ({
  ref,
  state: "ready",
  title,
  type,
  status: "active",
  confidence: null,
  tags,
  created: date(age + 20),
  modified: date(age),
  summary,
  links,
  complete: true,
});
const overview: VaultOverview = {
  notes: [
    overviewRow(
      "30-knowledge/summary.md",
      "A useful summary",
      "A concise authored summary for this workspace.",
      0,
      ["planning"],
    ),
    overviewRow(
      "30-knowledge/old-knowledge.md",
      "Old Knowledge",
      "An older note worth rediscovering.",
      45,
      ["research"],
    ),
    overviewRow(
      "50-maps/research-map.md",
      "Research map",
      "Map of related research notes.",
      3,
      ["research"],
      "map",
      2,
    ),
  ],
  partial: false,
  partial_reasons: [],
  omitted: 0,
  read_bytes: 2048,
};
const noteSource = `---\ntitle: A useful summary\ntags: [planning]\nsummary: A concise authored summary for this workspace.\n---\n\n# A useful summary\n\nThis synthetic article has a clear opening paragraph and enough detail to inspect its reader layout.\n\n## Next steps\n\nKeep the exact note reference visible while related notes, backlinks and history remain available below.\n`;
const note: VaultNote = {
  ref: "30-knowledge/summary.md",
  title: "A useful summary",
  text: noteSource,
  source: noteSource,
  byte_size: new TextEncoder().encode(noteSource).length,
  sha256: "a".repeat(64),
  frontmatter: {
    title: "A useful summary",
    tags: "[planning]",
    summary: "A concise authored summary for this workspace.",
  },
  frontmatter_error: null,
  links: ["50-maps/research-map.md"],
};

interface VoicePackWire {
  state: "not_installed";
  reason: null;
  error: null;
  progress: null;
  download_bytes: number;
  disk_bytes: number;
  need_bytes: number;
  free_bytes: null;
  gpu: null;
  kokoro: boolean;
  speed: { kokoro: null; qwen: null };
  fallbacks: [];
}

let server: ViteDevServer | undefined;
let browser: Browser | undefined;

beforeAll(async () => {
  mkdirSync(OUT, { recursive: true });
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

function answer(
  path: string,
  searchParams: URLSearchParams,
  theme: Theme,
  fontScale: number,
): unknown | undefined {
  if (path === "/api/v2/capabilities")
    return {
      app: { mode: "normal" },
      workspace: {
        active: {
          id: WORKSPACE,
          kind: "vault",
          name: "Field Notes",
          policy: "ordinary",
          layout: "unknown",
          source: "registration",
          aliased: false,
          root_available: true,
        },
        chat_only: false,
        selected_id: WORKSPACE,
        selected_by: "registration",
        restart_required: false,
        shared_scratch: false,
      },
      provider: {
        claude: {
          cli: "missing",
          version: "",
          login: "unknown",
          billing: "unknown",
          last_checked_at: null,
          can_start: false,
          problems: [],
          source: "",
          version_status: "unknown",
          tested: ["", ""],
        },
      },
      git: { state: "missing", version: "" },
      storage: { state: "ready", reason: null, private: true },
      features: {},
      launch: { id: "synthetic" },
    } satisfies Capabilities;
  if (path === "/api/v2/workspaces") {
    const workspace: WorkspacesListing["workspaces"][number] = {
      workspace_id: WORKSPACE,
      kind: "vault",
      root: "<vault>",
      root_available: true,
      root_physical_id: "1:2",
      aliased: false,
      name: "Field Notes",
      policy: "ordinary",
      policy_revision: 1,
      revision: 1,
      source: "registration",
      layout: "unknown",
      portable_id: null,
      identity: { established: false, git_root_oids: 0, inherits_from: null, probed_at: stamp(0) },
      face_dir: null,
      bootstrap: "complete",
      persistent: true,
      created_at: stamp(30),
      updated_at: stamp(0),
    };
    return {
      workspaces: [workspace],
      active_id: WORKSPACE,
      selected_id: WORKSPACE,
      selected_by: "registration",
      chat_only: false,
      scratch: {
        workspace_id: "33333333-3333-4333-8333-333333333333",
        kind: "scratch",
        shared: true,
      },
    } satisfies WorkspacesListing;
  }
  if (path === "/api/v2/settings")
    return {
      version: 1,
      revision: 1,
      read_only: false,
      reason: null,
      values: {},
      effective: {
        "appearance.theme": theme,
        "appearance.density": "comfortable",
        "accessibility.font_scale": fontScale,
      },
      sources: {},
      keys: {},
    } satisfies SettingsView;
  if (path === "/api/v2/folders/pick")
    return {
      available: false,
      multiple: false,
      reason: "unsupported",
    } satisfies FolderPickerStatus;
  if (path === "/api/v2/voice/pack")
    return {
      pack: {
        state: "not_installed",
        reason: null,
        error: null,
        progress: null,
        download_bytes: 0,
        disk_bytes: 0,
        need_bytes: 0,
        free_bytes: null,
        gpu: null,
        kokoro: false,
        speed: { kokoro: null, qwen: null },
        fallbacks: [],
      },
    } satisfies { pack: VoicePackWire };
  if (path === "/api/v2/voice")
    return {
      voice: {
        state: "setup_needed",
        running: false,
        model: null,
        backend: "system",
        device: null,
        languages: [],
        language: "en",
        language_unavailable: false,
        models: [],
        backends: [],
        fallback: null,
        cpu_model: null,
        slow_on_cpu: false,
      },
    } satisfies { voice: VoiceStatus };
  if (path === "/api/v2/conversation-archives") return { archives: [] satisfies ArchiveSummary[] };
  if (path === "/api/v2/attachment-capabilities")
    return {
      provider: "claude",
      media_types: [...ACCEPTED_MEDIA_TYPES],
      max_files: MAX_ATTACHMENTS,
      max_file_bytes: MAX_FILE_BYTES,
      max_turn_bytes: MAX_TURN_BYTES,
      max_dimension: 40_000_000,
    };
  if (/^\/api\/v2\/sessions\/[0-9a-f-]{36}\/prefs$/.test(path))
    return { model: null, effort: "medium" } satisfies ChatPrefs;
  if (path === "/api/v2/providers/claude/models")
    return {
      provider: "claude",
      adapter_version: "profile-v1",
      native_version: null,
      observed_at: null,
      catalog_key: null,
      status: "unverified",
      models: [],
    } satisfies ProviderModelView;
  if (path === "/api/v2/llm/connections")
    return { revision: 0, connections: [], default_model: null } satisfies ModelConnectionsListing;
  if (path === "/api/v2/native-assets") {
    const sourceId = searchParams.get("source_id");
    if (sourceId === null) return { sources: [] satisfies NativeSource[] };
    if (sourceId !== `claude:workspace:${WORKSPACE}`) return undefined;
    return {
      source_id: sourceId,
      provider: "claude",
      source_scope: "workspace",
      assets: [],
      partial: false,
      partial_reasons: [],
      scanned_entries: 0,
      scanned_bytes: 0,
      omitted: 0,
    } satisfies NativeCatalog;
  }
  if (path === "/api/v2/session-metadata") return { sessions: [] satisfies SessionMetadata[] };
  if (path === "/api/v2/routines")
    return {
      routines: [],
      scheduler: { state: "stopped", attended: false, paused: false } satisfies SchedulerStatus,
    };
  if (path === "/api/v2/session-roots") return { roots: [], truncated: false };
  if (path === "/api/v2/worktrees") return { worktrees: [], truncated: false };
  if (path === "/api/v2/decisions") return { items: [] } satisfies DecisionInbox;
  if (path === "/api/v2/bots")
    return {
      bots: [],
      scheduler: { state: "stopped", attended: false, paused: false },
    } satisfies BotsList;
  if (path === "/api/v2/limits")
    return { observation: null } satisfies { observation: PlanLimitReading | null };
  if (path === `/api/v2/vaults/${WORKSPACE}/memories`)
    return { items: [], invalid: [], partial: false } satisfies MemoryListing;
  if (path === `/api/v2/vaults/${WORKSPACE}/recovery`)
    return { entries: [], stuck: [] } satisfies RecoveryList;
  if (path === "/api/v2/vault-publications/recovery") return { publications: [] };
  if (path === `/api/v2/vaults/${WORKSPACE}/entries`)
    return { entries: listing, partial: false, partial_reasons: [], omitted: 0 };
  if (path === `/api/v2/vaults/${WORKSPACE}/overview`) return overview;
  if (path === `/api/v2/vaults/${WORKSPACE}/bookmarks`)
    return {
      bookmarks: [
        { ref: "30-knowledge/summary.md", created_at: stamp(5) },
        { ref: "missing/moved.md", created_at: stamp(2) },
      ] satisfies VaultBookmark[],
    };
  if (path === `/api/v2/vaults/${WORKSPACE}/captures`)
    return {
      captures: [
        {
          ref: "00-inbox/capture.md",
          revision: "b".repeat(64),
          modified: stamp(1),
          captured: "A thought from the inbox",
          preview: "A captured idea ready to sort.",
          cut: false,
          unread: null,
          suggestion: null,
        },
      ],
      projects: [],
      partial: false,
      omitted: 0,
      run: null,
    } satisfies CaptureListing;
  if (path === `/api/v2/vaults/${WORKSPACE}/notes/30-knowledge/summary.md`)
    return {
      note,
      resolved_links: [
        {
          target: "50-maps/research-map.md",
          state: "resolved",
          candidates: ["50-maps/research-map.md"],
        },
      ],
      partial: false,
      partial_reasons: [],
    };
  if (path === `/api/v2/vaults/${WORKSPACE}/backlinks/30-knowledge/summary.md`)
    return {
      backlinks: [{ source: "50-maps/research-map.md", line: 4 }],
      partial: false,
      partial_reasons: [],
      omitted: 0,
    };
  if (path === `/api/v2/vaults/${WORKSPACE}/history/30-knowledge/summary.md`)
    return {
      commits: [{ short: "f19c02a", at: stamp(3), subject: "Add summary and map links" }],
      state: "ready",
    } satisfies VaultNoteHistory;
  if (path === `/api/v2/vaults/${WORKSPACE}/notes/50-maps/research-map.md`)
    return {
      note: {
        ...note,
        ref: "50-maps/research-map.md",
        title: "Research map",
        text: "# Research map\n\nA synthetic map note.",
        source: "# Research map\n\nA synthetic map note.",
        frontmatter: {},
        links: [],
      },
      resolved_links: [],
      partial: false,
      partial_reasons: [],
    };
  if (path === `/api/v2/vaults/${WORKSPACE}/backlinks/50-maps/research-map.md`)
    return { backlinks: [], partial: false, partial_reasons: [], omitted: 0 };
  if (path === `/api/v2/vaults/${WORKSPACE}/history/50-maps/research-map.md`)
    return { commits: [], state: "ready" } satisfies VaultNoteHistory;
  if (path === "/api/rails")
    return {
      agents: [],
      gates: [],
      routines: [],
      counts: { knowledge: 4, sources: 0, inbox: 1 },
      git: {
        state: "not_repository",
        ref_state: "unknown",
        branch: "",
        head: "",
        committed_at: "",
        dirty: null,
      },
      counts_state: "ready",
    } satisfies Rails;
  if (path === "/api/sessions")
    return { sessions: [], state: "ready", reason: "", omitted: 0 } satisfies SessionsResult;
  if (path === "/api/v2/drafts") return { drafts: [], next_after: null } satisfies DraftPage;
  if (/^\/api\/v2\/drafts\/[0-9a-f-]{36}$/.test(path)) {
    const key = path.slice("/api/v2/drafts/".length);
    return { key, revision: 0, text: "", saved_at: null } satisfies Draft;
  }
  return undefined;
}

type Theme = "light" | "dark";
interface Size {
  width: number;
  height: number;
}
const SIZES: readonly Size[] = [
  { width: 1440, height: 1000 },
  { width: 400, height: 700 },
];
const THEMES: readonly Theme[] = ["light", "dark"];
const unmatchedRestRoutes = new WeakMap<Page, string[]>();

async function open(size: Size, theme: Theme, fontScale = 1): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: size, colorScheme: theme });
  unmatchedRestRoutes.set(page, []);
  page.on("response", (response) => {
    if (response.status() === 501) unmatchedRestRoutes.get(page)?.push(response.url());
  });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const body = answer(url.pathname, url.searchParams, theme, fontScale);
    if (body === undefined) {
      await route.fulfill({
        status: 501,
        contentType: "text/plain",
        body: `Unmatched synthetic route: ${route.request().method()} ${url.pathname}`,
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
  await page.goto(`${ORIGIN}/#token=vault-shots`);
  await page.locator('[data-region="shell"]').waitFor({ timeout: 15_000 });
  await page.waitForFunction((value) => document.documentElement.dataset.theme === value, theme);
  await navigateSidebar(page, "Vault");
  await page.getByRole("region", { name: "Vault browser" }).waitFor({ timeout: 15_000 });
  await page.getByRole("button", { name: "Front page", exact: true }).waitFor({ timeout: 15_000 });
  await page.waitForFunction(
    (value) => document.documentElement.style.getPropertyValue("--font-scale") === String(value),
    fontScale,
  );
  await page.evaluate(() => document.fonts.ready);
  return page;
}

async function capture(view: string, ready: (page: Page) => Promise<void>): Promise<void> {
  for (const size of SIZES)
    for (const theme of THEMES) {
      const page = await open(size, theme);
      try {
        await ready(page);
        assertNoUnmatchedVaultRoutes(page);
        await page.evaluate(() => document.fonts.ready);
        await page.screenshot({ path: `${OUT}/vault-${view}-${size.width}-${theme}.png` });
      } finally {
        await page.close();
      }
    }
}

function assertNoUnmatchedVaultRoutes(page: Page): void {
  const unmatched = unmatchedRestRoutes.get(page) ?? [];
  if (unmatched.length) throw new Error(`Unmatched REST routes: ${unmatched.join(", ")}`);
}

test("front page, Knowledge, Everything else and article reader at both sizes and themes", async () => {
  await capture("front", async (page) => {
    await page.getByRole("region", { name: "Vault overview" }).waitFor();
    await page.getByText("1 capture waiting", { exact: true }).waitFor();
    await page.getByText("Not in the vault listing", { exact: true }).waitFor();
    if (page.viewportSize()?.width === 1440) {
      const gap = await page.evaluate(() => {
        const featured = document.querySelector(".vo-featured");
        const recent = document.querySelector(".vo-recent");
        if (!featured || !recent) throw new Error("Vault overview cards are not rendered");
        return recent.getBoundingClientRect().top - featured.getBoundingClientRect().bottom;
      });
      if (gap < 0 || gap > 32) throw new Error(`Front page recent gap is outside 0–32px: ${gap}px`);
    }
  });
  await capture("knowledge", async (page) => {
    await page.getByRole("button", { name: "Knowledge", exact: true }).click();
    await page.getByRole("heading", { name: "Knowledge", level: 1, exact: true }).waitFor();
    await page
      .locator('button[data-vault-kind="row"][data-vault-ref="30-knowledge/old-knowledge.md"]')
      .waitFor({ state: "visible" });
    await page.getByText("30-knowledge/old-knowledge.md", { exact: true }).waitFor();
  });
  await capture("else", async (page) => {
    await page.getByRole("button", { name: "Everything else", exact: true }).click();
    await page.getByRole("heading", { name: "Everything else", level: 1, exact: true }).waitFor();
  });
  await capture("reader", async (page) => {
    await page
      .locator('button[data-vault-kind="lead"][data-vault-ref="30-knowledge/summary.md"]')
      .click();
    await page.getByRole("article").getByRole("heading", { name: "A useful summary" }).waitFor();
    await waitForBookmarkedReader(page);
  });
}, 300_000);

test("article reader at font scale 1.5 in both themes", async () => {
  for (const theme of THEMES) {
    const page = await open({ width: 400, height: 700 }, theme, 1.5);
    try {
      await page
        .locator('button[data-vault-kind="lead"][data-vault-ref="30-knowledge/summary.md"]')
        .click();
      await page.getByRole("article").getByRole("heading", { name: "A useful summary" }).waitFor();
      await waitForBookmarkedReader(page);
      assertNoUnmatchedVaultRoutes(page);
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: `${OUT}/vault-reader-400-${theme}-scale-1_5.png` });
    } finally {
      await page.close();
    }
  }
}, 120_000);

async function waitForBookmarkedReader(page: Page): Promise<void> {
  await page.waitForFunction(() => {
    const button = document.querySelector<HTMLButtonElement>(".vault-reader-bookmark");
    return (
      button !== null &&
      !button.disabled &&
      button.getAttribute("aria-pressed") === "true" &&
      button.getAttribute("aria-label") === "Remove bookmark 30-knowledge/summary.md"
    );
  });
  await page.waitForFunction(() => {
    const markdown = document.querySelector(".vault-note-content > .md");
    return (
      markdown !== null &&
      !markdown.hasAttribute("aria-busy") &&
      [...markdown.querySelectorAll("h2")].some((heading) => heading.textContent === "Next steps")
    );
  });
  if (page.viewportSize()?.width === 400) {
    const overflow = await page.evaluate(() => {
      const article = document.querySelector(".vault-inspector");
      if (!article) throw new Error("Vault article is not rendered");
      return {
        document: document.documentElement.scrollWidth - window.innerWidth,
        article: article.scrollWidth - article.clientWidth,
      };
    });
    if (overflow.document > 1 || overflow.article > 1)
      throw new Error(`Reader has horizontal overflow at 400px: ${JSON.stringify(overflow)}`);
  }
}
