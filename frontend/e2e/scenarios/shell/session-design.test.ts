/** Actual App with synthetic REST/socket data. No provider, vault or screenshots. */
import type { Browser, Page } from "playwright-core";
import { createServer, type ViteDevServer } from "vite";
import { afterAll, beforeAll, expect, test } from "vitest";
import * as events from "../../../src/accept/m2b/fakes";
import { GENERATION, sessionRow, snapshotData } from "../../../src/accept/m3b/fakes";
import type { SessionMetadata } from "../../../src/clients/conversations/sessionMetadataApi";
import type { SnapshotItem } from "../../../src/protocol";
import { capabilitiesFixture, settingsFixture } from "../../../src/testShell";
import {
  ACCEPTED_MEDIA_TYPES,
  MAX_ATTACHMENTS,
  MAX_FILE_BYTES,
  MAX_TURN_BYTES,
} from "../../../src/turnInput";
import { launchEdge } from "../../harness";
import { sidebarNavigation } from "../../sidebar-navigation";

const PORT = 5423;
const ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
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

async function open(width: number, height: number, scale = 1, dirty = false) {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width, height } });
  let metadata: SessionMetadata = {
    session_id: ID,
    title: "Design session",
    revision: 1,
    pinned: false,
    archived: false,
  };
  let update: (events: SnapshotItem[]) => void = () => {
    throw new Error("no subscribed session");
  };
  // Closing asks nothing (App.tsx requestClose): the real proof a click reached Close is the
  // close request it sends, mirroring closeChat.unit.test.tsx's own closeSends helper.
  const closeRequests: string[] = [];
  await page.routeWebSocket("**/ws", (socket) => {
    socket.onMessage((raw) => {
      const message = JSON.parse(String(raw));
      if (message.type === "session.close") {
        closeRequests.push(String(message.session_id));
        return;
      }
      if (message.type !== "session.open") return;
      let seq = 0;
      update = (events) => {
        const identity = {
          v: 2,
          session_id: ID,
          generation: GENERATION,
          request_id: message.request_id,
          seq: ++seq,
        };
        socket.send(
          JSON.stringify({
            ...identity,
            type: "session.snapshot",
            data: { ...snapshotData(), events, state: { ...snapshotData().state, live: true } },
          }),
        );
        socket.send(JSON.stringify({ ...identity, type: "session.synced", data: {} }));
      };
      update([]);
    });
  });
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === `/api/v2/session-metadata/${ID}` && route.request().method() === "PATCH") {
      const input = route.request().postDataJSON();
      metadata = { ...metadata, ...input.patch, revision: metadata.revision + 1 };
      return route.fulfill({ json: metadata });
    }
    const bodies: Record<string, unknown> = {
      "/api/sessions": {
        sessions: [sessionRow({ session_id: ID, title: "Design session" })],
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
          dirty: dirty ? 1 : 0,
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
      "/api/v2/capabilities": capabilitiesFixture(),
      "/api/v2/settings": settingsFixture({
        "onboarding.completed": true,
        "appearance.theme": "dark",
        "accessibility.font_scale": scale,
      }),
      "/api/v2/session-metadata": { sessions: [metadata] },
      "/api/v2/drafts": { drafts: [], next_after: null },
      "/api/v2/routines": {
        routines: [],
        scheduler: { state: "running", attended: true, paused: false },
      },
    };
    const key = /^\/api\/v2\/drafts\/(.+)$/.exec(path)?.[1];
    const body = key ? { key, revision: 0, text: "", saved_at: null } : bodies[path];
    return route.fulfill({ status: body === undefined ? 404 : 200, json: body ?? {} });
  });
  await page.goto(`http://127.0.0.1:${PORT}/#token=session-design-synthetic`);
  await page.locator(".empty-brand").waitFor();
  await page.waitForFunction(
    (wanted) =>
      document.documentElement.style.getPropertyValue("--font-scale") === wanted &&
      getComputedStyle(document.documentElement).zoom === wanted,
    String(scale),
  );
  await page.evaluate(async () => {
    await document.fonts.ready;
  });
  return { page, update: (events: SnapshotItem[]) => update(events), closeRequests };
}

async function rail(page: Page) {
  await sidebarNavigation(page);
  return page.locator(`[data-session-id="${ID}"]`);
}

test.each([
  [1440, 900, 1],
  [400, 700, 1],
  [400, 700, 1.5],
  [320, 600, 1],
])("organized menu and keyboard rename fit %s×%s at scale %s", async (width, height, scale) => {
  const { page } = await open(width, height, scale);
  try {
    const brand = page.locator(".empty-brand");
    expect(await brand.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    const row = await rail(page);
    const trigger = row.getByRole("button", { name: "Session menu" });
    await trigger.focus();
    await page.keyboard.press("ArrowDown");
    const menu = page.getByRole("menu");
    await menu.waitFor();
    const fit = await menu.evaluate((node) => {
      const box = node.getBoundingClientRect();
      return (
        box.left >= -1 &&
        box.top >= -1 &&
        box.right <= innerWidth + 1 &&
        box.bottom <= innerHeight + 1 &&
        node.scrollWidth <= node.clientWidth + 1
      );
    });
    expect(fit).toBe(true);
    expect(await menu.evaluate((node) => !node.closest("[inert]"))).toBe(true);
    await menu.getByRole("menuitem", { name: "Rename…" }).focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "Rename session" });
    await dialog.waitFor();
    expect(await dialog.evaluate((node) => !node.closest("[inert]"))).toBe(true);
    await dialog.getByRole("textbox", { name: "Session name" }).fill("Renamed session");
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "detached" });
    const destination = page.getByRole("button", { name: "Sessions and agents" });
    expect(await page.evaluate(() => document.activeElement?.getAttribute("aria-label"))).toBe(
      (await destination.isVisible()) ? "Sessions and agents" : "Session menu",
    );
    const current = await rail(page);
    await current.getByRole("button", { name: "Session menu" }).click();
    await page.getByRole("menuitem", { name: "Pin", exact: true }).click();
    await page
      .locator(".sess-group")
      .filter({ hasText: "Pinned" })
      .locator(`[data-session-id="${ID}"]`)
      .waitFor();
    expect(await page.locator(".sess-selected").count()).toBe(0);
  } finally {
    await page.close();
  }
});

test.each([
  [1440, 900, 1],
  [1920, 1080, 1],
  [400, 700, 1.5],
  [320, 600, 1],
  [320, 600, 1.5],
  [1280, 450, 1.5],
])(
  "new-conversation wordmark is prominent and fits %s×%s at scale %s",
  async (width, height, scale) => {
    const { page } = await open(width, height, scale);
    try {
      const probe = await page.locator(".empty-brand").evaluate((brand) => {
        const word = brand.querySelector<HTMLElement>(".empty-wordmark");
        const tiles = word?.querySelector<SVGSVGElement>("svg.tile-wordmark-large");
        const pane = brand.closest<HTMLElement>(".conversation");
        if (!word || !tiles || !pane) throw new Error("missing new-chat branding");
        const paneBox = pane.getBoundingClientRect();
        const tileBox = tiles.getBoundingClientRect();
        return {
          text: word.textContent?.replace(/\s/g, ""),
          label: word.getAttribute("aria-label"),
          tileCount: tiles.querySelector("path")?.getAttribute("d")?.split("M").length ?? 0,
          height: tileBox.height,
          width: tileBox.width,
          paneWidth: paneBox.width,
          fits: [...brand.querySelectorAll<Element>(".tile-wordmark, .empty-acronym")].every(
            (node) => {
              const box = node.getBoundingClientRect();
              return (
                box.left >= paneBox.left - 1 &&
                box.right <= paneBox.right + 1 &&
                node.scrollWidth <= node.clientWidth + 1
              );
            },
          ),
          horizontalScroll: pane.scrollWidth > pane.clientWidth + 1,
        };
      });
      expect(probe.text).toBe("VIKIAGENT");
      expect(probe.label).toBe("VIKI Agent");
      expect(probe.tileCount).toBeGreaterThan(800);
      expect(probe.fits).toBe(true);
      expect(probe.horizontalScroll).toBe(false);
      if (width >= 1440 && scale === 1) {
        expect(probe.height, JSON.stringify(probe)).toBeGreaterThanOrEqual(60);
        expect(probe.width, JSON.stringify(probe)).toBeGreaterThan(probe.paneWidth * 0.6);
      }
      const composer = await page
        .getByRole("textbox", { name: "Message", exact: true })
        .boundingBox();
      if (!composer) throw new Error("composer is not visible");
      expect(composer.y + composer.height).toBeLessThanOrEqual(height + 1);
    } finally {
      await page.close();
    }
  },
);

test("three warm edge motions render and reduced motion/forced colors disable them", async () => {
  const { page, update } = await open(1440, 900);
  try {
    await (await rail(page)).click();
    const started: SnapshotItem = { type: "turn.started", data: { turn_id: "synthetic-turn" } };
    const done: SnapshotItem = {
      type: "turn.done",
      data: {
        status: "success",
        usage: {
          input_tokens: 0,
          output_tokens: 0,
          cache_read_tokens: 0,
          cache_creation_tokens: 0,
        },
        context_percent: null,
        duration_ms: 0,
      },
    };
    const waiting: SnapshotItem = {
      type: "question.asked",
      data: {
        tool_use_id: "q1",
        questions: [{ header: "Choice", question: "Continue?", options: [], multi_select: false }],
      },
    };
    for (const [status, events, name, duration] of [
      ["running", [started], "sess-edge-trace", "3.2s"],
      ["needs-you", [started, waiting], "sess-edge-attention", "6.8s, 4.5s"],
      ["done", [started, done], "sess-edge-trace", "11s"],
    ] as const) {
      update([...events]);
      const edge = page.locator(`.sess-activity[data-activity="${status}"]`);
      await edge.waitFor();
      expect(
        await edge.evaluate((node) => getComputedStyle(node, "::before").animationName),
      ).toContain(name);
      expect(
        await edge.evaluate((node) => getComputedStyle(node, "::before").animationDuration),
      ).toBe(duration);
      const angles = await edge.evaluate(async (node) => {
        const animations = node.getAnimations({ subtree: true });
        for (const animation of animations) {
          animation.pause();
          animation.currentTime = 0;
        }
        const first = getComputedStyle(node, "::before").getPropertyValue("--viki-edge-turn");
        for (const animation of animations) animation.currentTime = 2000;
        await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
        return [first, getComputedStyle(node, "::before").getPropertyValue("--viki-edge-turn")];
      });
      expect(angles[0]).not.toBe(angles[1]);
    }
    await page.emulateMedia({ reducedMotion: "reduce" });
    expect(
      await page
        .locator(".sess-activity")
        .evaluate((node) => getComputedStyle(node, "::before").animationName),
    ).toBe("none");
    await page.emulateMedia({ forcedColors: "active" });
    expect(await page.locator(".sess-activity").isVisible()).toBe(false);
  } finally {
    await page.close();
  }
});

test("held session Close remains pointer-accessible in a short scaled drawer", async () => {
  const { page, update, closeRequests } = await open(400, 450, 1.5, true);
  try {
    await (await rail(page)).click();
    update([
      events.userMessage("Synthetic write"),
      events.turnStarted,
      events.toolStarted("write", "Write", { head: "Synthetic write", rest: "", tail: "" }),
      events.toolDone("write"),
      events.turnDone(),
    ] as unknown as SnapshotItem[]);
    const row = await rail(page);
    // The meta line also holds the row's ⋯ trigger, so no element's whole text is an exact
    // match (9a3a9d84 replaced the Uncommitted chip with this line).
    await row.locator(".m", { hasText: "live · idle" }).waitFor();
    await row.getByRole("button", { name: "Session menu" }).press("Enter");
    const close = page.getByRole("menuitem", { name: "Close session", exact: true });
    await close.focus();
    const probe = await close.evaluate(async (node) => {
      await new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      );
      const box = node.getBoundingClientRect();
      const panel = node.closest('[role="menu"]')?.getBoundingClientRect();
      const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
      return {
        hit: node.contains(hit),
        target: hit?.className,
        box: box.toJSON(),
        panel: panel?.toJSON(),
      };
    });
    expect(probe.hit, JSON.stringify(probe)).toBe(true);
    // A move as its own awaited step, before the click: measured directly (scratch run), a bare
    // page.mouse.click() at this exact point sends no session.close, while this two-step sequence
    // does — the Dropdown.Item's onSelect needs the pointer-driven highlight a separate move
    // commits to settle first; the earlier .focus() above only moves native focus, not that.
    await page.mouse.move(probe.box.x + probe.box.width / 2, probe.box.y + probe.box.height / 2);
    await page.mouse.click(probe.box.x + probe.box.width / 2, probe.box.y + probe.box.height / 2);
    // Close asks nothing (App.tsx requestClose): the real click's own proof is the close request
    // it sends, the same signal closeChat.unit.test.tsx's closeSends helper reads. The poll gets
    // this file's 15 s budget, not Vitest's 1 s default, which a slow runner can miss.
    await expect.poll(() => closeRequests, { timeout: 15_000 }).toEqual([ID]);
  } finally {
    await page.close();
  }
});
