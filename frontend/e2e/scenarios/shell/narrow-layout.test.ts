/**
 * The narrow and zoomed layouts, in real Edge against the Vite dev server with every
 * REST route answered from the synthetic data below — the same shape layout.accept.test.ts and
 * screenshots.shots.ts use, with the socket down and no backend. Nothing here binds 8765.
 *
 * What only a browser can answer is measured here: whether anything overflows its box, whether
 * the composer's controls still lie inside the pill, whether `inert` really takes the background
 * out of the tab order (jsdom sets the attribute and implements none of its behaviour), and
 * whether the drawer's animation is off under reduced motion. The DOM-level contract — what a
 * drawer renders, what closes it, where focus goes — is pinned in jsdom by
 * src/components/Shell/RailDrawer.unit.test.tsx.
 *
 * The 200% zoom case is 720x450 at deviceScaleFactor 2: a zoomed 1440x900 window reports exactly
 * that to CSS. Real Ctrl+Plus in Edge is a manual walkthrough, not this harness's.
 *
 * Run from frontend/:
 *   npx vitest run --config vitest.e2e.config.ts e2e/scenarios/shell/narrow-layout.test.ts
 */
import type { Browser, Locator, Page } from "playwright-core";
import { createServer, type ViteDevServer } from "vite";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import type { Rails, SearchResult, SessionRow, SessionsResult } from "../../../src/protocol";
import { capabilitiesFixture, FAKE_WORKSPACE_ID, settingsFixture } from "../../../src/testShell";
import { launchEdge } from "../../harness";

const PORT = 5197;
const ORIGIN = `http://127.0.0.1:${PORT}`;
let server: ViteDevServer | undefined;
let browser: Browser | undefined;

const DAY = 24 * 60 * 60 * 1000;
const now = Date.now();
const at = (daysAgo: number, hour: number, minute: number): string => {
  const d = new Date(now - daysAgo * DAY);
  d.setHours(hour, minute, 0, 0);
  return d.toISOString();
};

const row = (id: string, title: string, modified: string): SessionRow => ({
  session_id: id,
  title,
  first_prompt: title,
  entrypoint: "viki-agent",
  modified,
  cli_version: "2.1.259",
  other: false,
});

/** The listing as an observation (rows plus state, reason and omitted count): a complete read of
 * three synthetic rows. Typed, so a change to the REST shape breaks this file at compile time
 * rather than at 10s of waitFor. */
const sessionsResult: SessionsResult = {
  sessions: [
    row("s1", "Weekly review draft", at(0, 18, 42)),
    row("s2", "Search palette taste round", at(0, 17, 20)),
    row("s3", "Reminder to buy milk", at(1, 9, 5)),
  ],
  state: "ready",
  reason: "",
  omitted: 0,
};

const emptySearch: SearchResult = {
  hits: [],
  state: "ready",
  reason: "",
  scanned: 0,
  total: 0,
  request_id: "",
};

/** A 42-character branch name: the git chip is the one field in the top bar whose width another
 * program decides, so the narrow bar is measured against a long one. */
const LONG_BRANCH = "feature/narrow-layout-and-rail-drawers-e2e";

const rails: Rails = {
  agents: [
    {
      name: "researcher",
      description: "Web research and ingestion",
      tools: ["WebFetch", "WebSearch", "Write"],
      model: "opus",
      last_run: at(0, 9, 12),
    },
  ],
  gates: [
    {
      file: "2026-09-10.md",
      id: "H1",
      item: "/recall skill",
      label: "medium",
      owner_applies: false,
    },
  ],
  routines: [{ when: "07:30", name: "/daily · morning brief", planned: true }],
  counts: { knowledge: 116, sources: 202, inbox: 1 },
  git: {
    state: "known",
    ref_state: "branch",
    branch: LONG_BRANCH,
    head: "c8868e6",
    committed_at: at(0, 19, 16),
    dirty: 3,
  },
};

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

interface OpenOptions {
  height?: number;
  deviceScaleFactor?: number;
  reducedMotion?: "reduce" | "no-preference";
  /** Fails the sessions read (500) so the status bar's `notice` field renders too ("offline ·
   * retrying"), without disturbing the rails read that feeds vaultCounts/lastCommit. */
  sessionsError?: boolean;
  /** Overrides the git chip's branch name (default LONG_BRANCH). */
  gitBranch?: string;
}

async function open(width: number, options: OpenOptions = {}): Promise<Page> {
  if (!browser) throw new Error("Edge did not launch");
  const {
    height = 900,
    deviceScaleFactor,
    reducedMotion,
    sessionsError = false,
    gitBranch,
  } = options;
  const page = await browser.newPage({
    viewport: { width, height },
    ...(deviceScaleFactor === undefined ? {} : { deviceScaleFactor }),
    ...(reducedMotion === undefined ? {} : { reducedMotion }),
  });
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    if (sessionsError && url.pathname === "/api/sessions") {
      return route.fulfill({ status: 500 });
    }
    let body: unknown = null;
    if (url.pathname === "/api/rails")
      body =
        gitBranch === undefined ? rails : { ...rails, git: { ...rails.git, branch: gitBranch } };
    else if (url.pathname === "/api/sessions") body = sessionsResult;
    else if (url.pathname === "/api/search") body = emptySearch;
    else if (url.pathname === "/api/v2/capabilities")
      body = capabilitiesFixture({
        workspace: {
          active: {
            id: FAKE_WORKSPACE_ID,
            kind: "vault",
            name: "Synthetic vault",
            policy: "ordinary",
            layout: "legacy_para",
            source: "created",
            aliased: false,
            root_available: true,
          },
          chat_only: false,
          shared_scratch: false,
          selected_id: FAKE_WORKSPACE_ID,
          selected_by: "registration",
        },
      });
    else if (url.pathname === "/api/v2/settings")
      body = settingsFixture({ "onboarding.completed": true });
    // The same valid wire bounds src/clients/media/assetsApi.unit.test.ts checks: a missing response adds an
    // attachment error paragraph above the pill and measures that error's height as layout.
    else if (url.pathname === "/api/v2/attachment-capabilities")
      body = {
        provider: "claude",
        media_types: ["image/png", "image/jpeg", "image/webp"],
        max_files: 10,
        max_file_bytes: 5 << 20,
        max_turn_bytes: 20 << 20,
        max_dimension: 8000,
      };
    // The routines list, answered empty: unanswered, the right rail's Routines section would show
    // its load failure, and the drawer measurements would be of a broken backend.
    else if (url.pathname === "/api/v2/routines")
      body = { routines: [], scheduler: { state: "running", attended: true, paused: false } };
    // The durable draft store, answered as an empty healthy one would answer. Without these two
    // the app's own /api/v2/drafts calls 404, the composer shows "Can't save the draft. The text
    // stays in this window." with a retry button, and every measurement and every screenshot here
    // is of a broken backend rather than of the layout.
    else if (url.pathname === "/api/v2/drafts") body = { drafts: [], next_after: null };
    else {
      const key = /^\/api\/v2\/drafts\/(.+)$/.exec(url.pathname)?.[1];
      if (key) body = { key, revision: 0, text: "", saved_at: null };
    }
    if (body === null) return route.fulfill({ status: 404 });
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
  const capabilities = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/v2/capabilities",
  );
  await page.goto(`${ORIGIN}/#token=narrow-layout`);
  await page.locator('[data-region="shell"]').waitFor({ timeout: 10_000 });
  await capabilities;
  await page.locator(".readiness-banner").waitFor({ state: "hidden" });
  await page.evaluate(() => document.fonts.ready);
  return page;
}

async function box(page: Page, selector: string) {
  const found = await page.locator(selector).first().boundingBox();
  if (!found) throw new Error(`${selector} is not rendered`);
  return found;
}

/** Whether the element is wider than the box it is drawn in — the browser's own overflow answer. */
async function overflows(page: Page, selector: string): Promise<boolean> {
  return page
    .locator(selector)
    .first()
    .evaluate((el) => el.scrollWidth > el.clientWidth + 1);
}

/** Wait until the drawer has stopped moving.
 *
 * The panel slides in over 160ms, and a bounding box read while it travels is not the layout's:
 * measured 17ms in, the left panel sits at x -249.6 of its 300px width and the right one 283.2px
 * to the right of where it lands. Three runs of the same assertion gave -161.7, -203.7 and
 * -249.6, which is what a time-dependent measurement looks like. So every geometry assertion
 * here waits for the panel's own animations to finish first — the same wait under reduced
 * motion, where there are none to wait for and this returns at once.
 *
 * `finished` rejects when an animation is cancelled (the panel unmounted mid-slide), so each one
 * is caught rather than failing the test with an AbortError. The race is a bound, not a sleep:
 * it never runs when the animation completes, and it turns "the slide never ended" into a
 * failed assertion a line later rather than a hung test. */
async function drawerSettled(page: Page): Promise<void> {
  await page
    .locator(".rail-drawer")
    .evaluate((el) =>
      Promise.race([
        Promise.all(
          el.getAnimations({ subtree: true }).map((a) => a.finished.catch(() => undefined)),
        ),
        new Promise((resolve) => setTimeout(resolve, 2_000)),
      ]),
    );
}

/** Open a drawer by the accessible name its toggle and its panel share, and let it settle. */
async function openDrawer(page: Page, label: string): Promise<Locator> {
  await page.getByRole("button", { name: label }).click();
  const drawer = page.getByRole("dialog", { name: label });
  await drawer.waitFor();
  await drawerSettled(page);
  // The panel's own content (a rail's session titles, a tile's label) must stay inside it too,
  // not just the window around it.
  expect(await overflows(page, ".rail-drawer")).toBe(false);
  return drawer;
}

async function focusedInsideDialog(page: Page): Promise<boolean> {
  return page.evaluate(
    () => document.activeElement?.closest('[role="dialog"][aria-modal="true"]') !== null,
  );
}

/** Everything that must hold in any S-width window, whatever the device scale factor. */
async function expectSmallLayout(page: Page, width: number): Promise<void> {
  // Nothing overflows the window, so no horizontal scrollbar appears under the composer.
  // document.documentElement.scrollWidth cannot see this: .shell is `overflow: hidden`, which
  // clips every descendant's overflow before it can reach the document, so that check passed
  // even with a region inside .shell overflowing its own box. Each region that
  // can hold untrusted-length text (a git branch, a session or conversation title, a status
  // field) is measured against its own box instead.
  expect(await overflows(page, '[data-region="statusbar"]')).toBe(false);
  expect(await overflows(page, '[data-region="conversation-header"]')).toBe(false);
  expect(await overflows(page, '[data-region="message-column"]')).toBe(false);
  expect(await page.locator('[data-region="left-rail"]').count()).toBe(0);
  expect(await page.locator('[data-region="right-rail"]').count()).toBe(0);
  expect(await page.locator(".topbar .tabs").count()).toBe(0);

  // The conversation is the window, on the 16px gutter.
  expect((await box(page, '[data-region="message-column"]')).width).toBe(width - 32);

  // The pill and its contents: nothing spills out of the composer or the bar, and Send is inside.
  expect(await overflows(page, ".composer")).toBe(false);
  expect(await overflows(page, ".topbar")).toBe(false);
  const composer = await box(page, '[data-region="composer"]');
  expect(composer.width).toBe(width - 32);
  const submit = await box(page, ".composer-submit");
  expect(submit.x).toBeGreaterThanOrEqual(composer.x);
  expect(submit.x + submit.width).toBeLessThanOrEqual(composer.x + composer.width);
  expect(submit.y).toBeGreaterThanOrEqual(composer.y);
  expect(submit.y + submit.height).toBeLessThanOrEqual(composer.y + composer.height);
}

describe("400 CSS pixels", () => {
  test("both rails are drawers and nothing overflows the window", async () => {
    const page = await open(400);
    await expectSmallLayout(page, 400);
    // The height budget below the pill is only the layout's if the app is not also reporting a
    // broken backend: an unanswered /api/v2/drafts puts "Can't save the draft" and a retry
    // button in the composer slot, which is chrome this file would then be measuring around.
    expect(await page.locator(".draft-status").textContent()).not.toContain("Can't save");
    await page.close();
  });

  test("opening a rail drawer does not scroll the shell sideways", async () => {
    // The regression this guards (useModalFocus.ts): entry focus runs in
    // useLayoutEffect, before the drawer's 160ms slide-in has painted a frame, so a plain
    // focus() on the off-screen Close button scrolled .shell (the nearest scroll container)
    // to reveal it, and everything else in the bar rode along until the slide caught up.
    // drawerSettled() (used by every other test here) waits the animation out first, so it
    // cannot see this: the read below happens inside the same synchronous click that runs
    // focusFirst(), before the animation has had a chance to decay the scroll back to 0.
    const page = await open(400, { reducedMotion: "no-preference" });
    // React does not commit the toggle's state update inside the synchronous click call stack
    // (a page.evaluate that clicks and reads back to back always read pre-commit, 0 either way
    // — confirmed by running this against the unfixed hook and getting a false pass). A
    // MutationObserver installed before the click fires as a microtask right after the drawer's
    // DOM lands, which is still before the 160ms slide has had a chance to decay the scroll
    // focusFirst() (useModalFocus.ts, run synchronously in the same commit) may have caused.
    const measured = await page.evaluate(() => {
      const shell = document.querySelector('[data-region="shell"]') as HTMLElement;
      const toggle = document.querySelector<HTMLElement>('[aria-label="VIKI panel"]');
      if (!toggle) throw new Error("VIKI panel toggle not rendered");
      return new Promise<{ scrollLeft: number; topbarX: number }>((resolve) => {
        const observer = new MutationObserver(() => {
          const dialog = document.querySelector('[role="dialog"]');
          if (!dialog) return;
          observer.disconnect();
          const topbar = document.querySelector(".topbar") as HTMLElement;
          resolve({ scrollLeft: shell.scrollLeft, topbarX: topbar.getBoundingClientRect().x });
        });
        observer.observe(document.body, { childList: true, subtree: true });
        toggle.click();
      });
    });
    expect(measured.scrollLeft).toBe(0);
    expect(measured.topbarX).toBe(0);
    await page.getByRole("dialog", { name: "VIKI panel" }).waitFor();
    await page.close();
  });

  test("the sessions drawer is a modal: focus enters it, the shell behind goes inert", async () => {
    const page = await open(400);
    const toggle = page.getByRole("button", { name: "Sessions and agents" });
    expect(await toggle.isVisible()).toBe(true);
    expect(await toggle.getAttribute("aria-expanded")).toBe("false");

    const drawer = await openDrawer(page, "Sessions and agents");
    expect(await toggle.getAttribute("aria-expanded")).toBe("true");
    const close = page.getByRole("button", { name: "Close" });
    expect(await close.evaluate((el) => el === document.activeElement)).toBe(true);

    // The drawer holds the rail itself, not a copy of part of it.
    expect(await drawer.locator('[data-region="left-rail"]').count()).toBe(1);
    expect(await drawer.locator("[data-session-id]").count()).toBeGreaterThan(0);

    // The shell's App menu is inert and the tab order cycles
    // inside the panel however long it is walked.
    expect(
      await page
        .locator('.topbar button[aria-label="App menu"]')
        .evaluate((el) => el.closest("[inert]") !== null),
    ).toBe(true);
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press("Tab");
      expect(await focusedInsideDialog(page)).toBe(true);
    }

    // The panel is a layer over the conversation, not the whole window.
    const panel = await box(page, ".rail-drawer");
    expect(panel.width).toBeLessThanOrEqual(400 - 48);
    expect(panel.x).toBe(0);

    await page.keyboard.press("Escape");
    await drawer.waitFor({ state: "detached" });
    expect(await toggle.evaluate((el) => el === document.activeElement)).toBe(true);
    expect(
      await page
        .locator('.topbar button[aria-label="App menu"]')
        .evaluate((el) => el.closest("[inert]") === null),
    ).toBe(true);
    await page.close();
  });

  test("the VIKI panel drawer carries the sections the right rail would have shown", async () => {
    const page = await open(400);
    const drawer = await openDrawer(page, "VIKI panel");
    expect(await drawer.locator(".tile").count()).toBe(3);
    const panel = await box(page, ".rail-drawer");
    expect(panel.x + panel.width).toBe(400);

    await page.getByRole("button", { name: "Close" }).click();
    await drawer.waitFor({ state: "detached" });
    await page.close();
  });

  test("a click on the backdrop beside the panel closes the drawer", async () => {
    // The panel is 300px wide, so 380 lands in the strip of backdrop the drawer leaves over the
    // conversation. This assertion exists in Edge and nowhere else: the backdrop is the panel's
    // PARENT rather than its sibling precisely so useModalFocus does not inert it — an inert
    // element takes no pointer events at all — and jsdom implements none of that, so the sibling
    // arrangement passes every unit test in RailDrawer.unit.test.tsx unchanged.
    const page = await open(400);
    const drawer = await openDrawer(page, "Sessions and agents");
    expect((await box(page, ".rail-drawer")).width).toBeLessThanOrEqual(380);

    await page.mouse.click(380, 400);
    await drawer.waitFor({ state: "detached" });
    expect(await page.getByRole("dialog").count()).toBe(0);
    await page.close();
  });

  test("a drawer slides in, unless the user asked for less motion", async () => {
    // The one test that must NOT wait for the slide: what it asks is whether there is one. The
    // declared animation-name and the animation the browser is actually running are both read,
    // because the first alone would pass on a rule that never animates anything.
    const moving = await open(400);
    await moving.getByRole("button", { name: "Sessions and agents" }).click();
    await moving.getByRole("dialog", { name: "Sessions and agents" }).waitFor();
    expect(
      await moving.locator(".rail-drawer").evaluate((el) => ({
        name: getComputedStyle(el).animationName,
        running: el.getAnimations().length,
      })),
    ).toEqual({ name: "rail-drawer-in", running: 1 });
    await moving.close();

    const still = await open(400, { reducedMotion: "reduce" });
    await still.getByRole("button", { name: "Sessions and agents" }).click();
    await still.getByRole("dialog", { name: "Sessions and agents" }).waitFor();
    expect(
      await still.locator(".rail-drawer").evaluate((el) => ({
        name: getComputedStyle(el).animationName,
        running: el.getAnimations().length,
      })),
    ).toEqual({ name: "none", running: 0 });
    await still.close();
  });
});

describe("a short window", () => {
  // The status bar wraps at S rather than clipping a field, which costs height the conversation
  // would otherwise have.
  test.each([
    [400, 600],
    [800, 600],
  ])("at %ix%i the composer stays in the window and the scroll keeps its room", async (w, h) => {
    // sessionsError renders the bar's `notice` field too, alongside vaultCounts and lastCommit
    // (already given by the rails fixture): three of the bar's five fields, which is the most
    // this static-fixture harness can drive without a live session — cliVersion and
    // contextPercent only ever arrive over a reconnect snapshot from a running claude.exe, and
    // this file's whole point (see header comment) is measuring layout with the socket down.
    const page = await open(w, { height: h, sessionsError: true });
    expect(
      await page
        .getByText("Image attachments are unavailable. Reopen this conversation to try again.", {
          exact: true,
        })
        .count(),
    ).toBe(0);
    const composer = await box(page, '[data-region="composer"]');
    expect(composer.y + composer.height).toBeLessThanOrEqual(h);
    expect(composer.x).toBeGreaterThanOrEqual(0);
    const scroll = await box(page, ".conversation-scroll");
    expect(scroll.height).toBeGreaterThanOrEqual(200);
    // Measured with vaultCounts + lastCommit + notice (three of the five fields, see above):
    // 37px at 400 (two wrapped rows under the S rule's flex-wrap) and 27px at 800 (the base
    // fixed-height rule, never wrapped — the M band has no flex-wrap at all, so this number is
    // the same whatever fields are present). A 60px bound could not fail: it is loose enough to
    // pass with only two fields rendered. 45px is real headroom above the measured 37/27 (font
    // metrics differ a few px across engines) while still failing if a wrap regression added a
    // third row (~52px+) here.
    expect((await box(page, '[data-region="statusbar"]')).height).toBeLessThanOrEqual(45);
    await page.close();
  });
});

describe("1024x768: the left rail stays docked, the right one is a drawer", () => {
  test("the design's left rail is untouched and the VIKI panel opens and closes", async () => {
    const page = await open(1024, { height: 768 });
    expect((await box(page, '[data-region="left-rail"]')).width).toBe(272);
    expect(await page.locator('[data-region="right-rail"]').count()).toBe(0);
    expect(await page.getByRole("button", { name: "Sessions and agents" }).count()).toBe(0);
    expect(await page.locator(".topbar .tabs").count()).toBe(0);
    expect(
      await page.getByRole("navigation", { name: "Navigation and sessions" }).isVisible(),
    ).toBe(true);

    const toggle = page.getByRole("button", { name: "VIKI panel" });
    const drawer = await openDrawer(page, "VIKI panel");
    expect(await drawer.locator(".tile").count()).toBe(3);
    // The docked rail behind it is inert too.
    expect(
      await page
        .locator('[data-region="left-rail"]')
        .evaluate((el) => el.closest("[inert]") !== null),
    ).toBe(true);

    await page.keyboard.press("Escape");
    await drawer.waitFor({ state: "detached" });
    expect(await toggle.evaluate((el) => el === document.activeElement)).toBe(true);
    await page.close();
  });
});

describe("800x700: the M band contains a long git chip", () => {
  // The chip's containment (min-width: 0, overflow: hidden, its text span's ellipsis) is a base
  // rule, not only the S (below-800px) media block's, so 800-1279 is guarded too. A single
  // unbroken branch name (no space, hyphen or slash for the browser's default line-breaking to
  // wrap at) has nothing to bend, so without the base-level containment it pushes the chip, then
  // .topbar, wider than the window instead of losing its tail to an ellipsis — the clearest,
  // most robust sign of the missing containment, whatever the exact font metrics in a given
  // engine.
  const UNBREAKABLE_BRANCH = "x".repeat(64);

  test("an unbreakable branch name ellipsizes instead of overflowing the bar", async () => {
    const page = await open(800, { height: 700, gitBranch: UNBREAKABLE_BRANCH });
    expect(await overflows(page, ".topbar")).toBe(false);
    expect(await overflows(page, ".git-chip")).toBe(false);
    await page.close();
  });
});

describe("200% zoom: 720x450 at device scale 2", () => {
  test("the window reports 720 CSS pixels and takes the S layout", async () => {
    const page = await open(720, { height: 450, deviceScaleFactor: 2 });
    expect(await page.evaluate(() => window.innerWidth)).toBe(720);
    expect(await page.evaluate(() => window.devicePixelRatio)).toBe(2);
    await expectSmallLayout(page, 720);

    const drawer = await openDrawer(page, "Sessions and agents");
    expect(await drawer.locator("[data-session-id]").count()).toBeGreaterThan(0);
    await page.keyboard.press("Escape");
    await drawer.waitFor({ state: "detached" });
    await page.close();
  });
});

describe("the window growing", () => {
  test("a drawer closes itself when the rail it holds docks again", async () => {
    const page = await open(400);
    await openDrawer(page, "Sessions and agents");

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.getByRole("dialog", { name: "Sessions and agents" }).waitFor({ state: "detached" });
    expect((await box(page, '[data-region="left-rail"]')).width).toBe(272);
    expect((await box(page, '[data-region="right-rail"]')).width).toBe(320);
    expect(await page.getByRole("button", { name: "VIKI panel" }).count()).toBe(0);
    await page.close();
  });
});
