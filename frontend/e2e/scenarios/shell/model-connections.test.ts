import type { Browser, Page } from "playwright-core";
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import type { ModelConnectionsListing } from "../../../src/clients/app/modelConnectionsApi";
import type { ClientMessage, ServerMessage } from "../../../src/protocol";
import { appUrl, launchEdge, send, TEST_TOKEN, waitForText } from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

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

async function providers(page: Page) {
  await page.keyboard.press("Control+Comma");
  await page.waitForSelector('[data-region="settings"]');
  await page.getByRole("tab", { name: "Providers", exact: true }).click();
  const advanced = page.locator(".llm-advanced");
  if (!(await advanced.evaluate((element) => element.hasAttribute("open"))))
    await advanced.locator("summary").click();
  await page.getByLabel("Connection name").waitFor();
}

async function api(method: string, path: string, body?: unknown) {
  const response = await fetch(`${ORIGIN}/api/v2/${path}`, {
    method,
    headers: {
      authorization: `Bearer ${TEST_TOKEN}`,
      origin: ORIGIN,
      "content-type": "application/json",
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  expect(response.status).toBe(200);
  return response;
}

async function waitForModelFocus(page: Page, ref: string) {
  await page.waitForFunction((value) => {
    const focused = document.activeElement;
    return (
      focused?.matches('.chip-menu [role="menuitem"][data-highlighted]') &&
      focused.getAttribute("title") === value
    );
  }, ref);
}

test("Magpie choices save through VIKI without direct fields or an automatic default change", async () => {
  server = await startServer({});
  if (!browser) throw new Error("Browser did not start");
  const page = await browser.newPage();
  const discoveries: unknown[] = [];
  let inferenceTests = 0;
  try {
    await page.route("**/api/v2/llm/magpie", (route) =>
      route.fulfill({
        json: {
          available: true,
          running: false,
          version: "0.1.3357",
          desktop: false,
          base_url: "http://127.0.0.1:3426/v1",
        },
      }),
    );
    await page.route("**/api/v2/llm/connections/models", (route) => {
      discoveries.push(route.request().postDataJSON());
      return route.fulfill({ json: { ok: true, models: ["synthetic/group", "__proto__"] } });
    });
    await page.route("**/api/v2/llm/connections/test", (route) => {
      inferenceTests++;
      return route.fulfill({ status: 503, json: { error: { code: "unavailable" } } });
    });
    await page.goto(appUrl());
    await page.waitForSelector('[data-region="shell"]');
    await page.keyboard.press("Control+Comma");
    await page.waitForSelector('[data-region="settings"]');
    await page.getByRole("tab", { name: "Providers", exact: true }).click();
    expect(await page.locator(".llm-advanced").getAttribute("open")).toBeNull();
    const before = await (await api("GET", "settings")).json();
    await page.getByRole("button", { name: "Choose a model or group", exact: true }).click();
    const picker = page.locator(".llm-magpie-picker");
    expect(await picker.getByLabel("API base URL").count()).toBe(0);
    expect(await picker.getByLabel("API key (optional for local servers)").count()).toBe(0);
    await picker.getByRole("button", { name: "List models", exact: true }).click();
    await picker
      .getByLabel("Magpie model or routing group", { exact: true })
      .selectOption("__proto__");
    const saving = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v2/llm/connections") &&
        response.request().method() === "POST",
    );
    await picker.getByRole("button", { name: "Add model to VIKI", exact: true }).click();
    expect((await saving).status()).toBe(200);
    // The saved listing is read from the server: the page reads this response as a stream, and
    // the browser need not keep a body it has already handed over.
    const savedListing = (await (
      await api("GET", "llm/connections")
    ).json()) as ModelConnectionsListing;
    const connection = savedListing.connections.find((item) => item.model === "__proto__");
    if (!connection) throw new Error("Magpie model was not saved");
    expect(connection).toMatchObject({
      name: "Magpie · __proto__",
      base_url: "http://127.0.0.1:3426/v1",
      api_mode: "anthropic",
      has_api_key: false,
    });
    expect(connection.model_ref).toMatch(/^viki-llm\/[0-9a-f-]{36}\/__proto__$/);
    expect((await (await api("GET", "settings")).json()).effective["chat.default_model"]).toBe(
      before.effective["chat.default_model"],
    );
    const row = page
      .locator(".llm-choice .llm-connection")
      .filter({ hasText: "Magpie · __proto__" });
    const choosing = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v2/settings") && response.request().method() === "PATCH",
    );
    await row.getByRole("button", { name: "Use for new chats", exact: true }).click();
    expect((await choosing).status()).toBe(200);
    await row.getByText("Default for new chats", { exact: true }).waitFor();
    await page.reload();
    await page.waitForSelector('[data-region="shell"]');
    await page.keyboard.press("Control+Comma");
    await page.waitForSelector('[data-region="settings"]');
    await page.getByRole("tab", { name: "Providers", exact: true }).click();
    await page.locator(".llm-current").getByText("Magpie · __proto__", { exact: true }).waitFor();
    expect(await page.locator(".llm-advanced").getAttribute("open")).toBeNull();
    expect(discoveries).toEqual([
      {
        connection: {
          name: "Magpie",
          base_url: "http://127.0.0.1:3426/v1",
          api_mode: "anthropic",
          model: "",
          api_key: "",
        },
      },
    ]);
    expect(inferenceTests).toBe(0);
  } finally {
    await page.close();
  }
});

test("a saved connection survives reload, clears keys and keeps a chat's saved preference after removal", async () => {
  server = await startServer({});
  if (!browser) throw new Error("Browser did not start");
  const page = await browser.newPage();
  try {
    await page.goto(appUrl());
    await page.waitForSelector('[data-region="shell"]');
    await providers(page);
    await page.getByLabel("Connection name").fill("My test connection");
    await page.getByLabel("API base URL").fill("https://models.example/v1");
    await page.getByLabel("API format").selectOption("chat_completions");
    await page.getByLabel("Model ID", { exact: true }).fill("example/model");
    await page.getByLabel("API key (optional for local servers)").fill("synthetic-only-key");
    const saving = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v2/llm/connections") &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Save connection", exact: true }).click();
    expect((await saving).status()).toBe(200);
    // Read from the server, as above: the browser need not keep this response's body.
    expect(await (await api("GET", "llm/connections")).text()).not.toContain("synthetic-only-key");
    const directRow = page
      .locator(".llm-advanced .llm-connection")
      .filter({ hasText: "My test connection" });
    await directRow.getByText("My test connection", { exact: true }).waitFor();
    expect(await page.getByLabel("API key (optional for local servers)").inputValue()).toBe("");
    const choosing = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v2/settings") && response.request().method() === "PATCH",
    );
    await page.getByRole("button", { name: "Use for new chats", exact: true }).click();
    expect((await choosing).status()).toBe(200);
    await directRow.getByText("Default for new chats", { exact: true }).waitFor();
    await page.reload();
    await page.waitForSelector('[data-region="shell"]');
    await providers(page);
    await directRow.getByText("My test connection", { exact: true }).waitFor();
    await directRow.getByText("Default for new chats", { exact: true }).waitFor();
    await page.locator(".llm-current").getByText("My test connection", { exact: true }).waitFor();
    const listing = (await (await api("GET", "llm/connections")).json()) as ModelConnectionsListing;
    const ref = listing.connections[0]?.model_ref;
    if (!ref) throw new Error("saved connection is missing");
    await navigateSidebar(page, "Chat");
    const chip = page.locator('.model-chip[aria-haspopup="menu"]');
    await chip.waitFor();
    await chip.click();
    await page
      .getByRole("menuitem")
      .filter({ hasText: "My test connection · example/model" })
      .click();
    const savedPrefs = page.waitForResponse(
      (response) =>
        /\/api\/v2\/sessions\/[^/]+\/prefs$/.test(response.url()) &&
        response.request().method() === "PATCH",
    );
    await send(page, "synthetic model preference check");
    const prefs = await savedPrefs;
    expect(prefs.status()).toBe(200);
    const prefsPath = new URL(prefs.url()).pathname.replace("/api/v2/", "");
    expect((await (await api("GET", prefsPath)).json()).model).toBe(ref);
    await providers(page);
    await directRow
      .getByRole("button", { name: "Remove connection: My test connection", exact: true })
      .click();
    await directRow.waitFor({ state: "detached" });
    await page.locator(".llm-current").getByText("Claude Code sign-in", { exact: true }).waitFor();
    expect((await (await api("GET", prefsPath)).json()).model).toBe(ref);
    expect(
      ((await (await api("GET", "llm/connections")).json()) as ModelConnectionsListing)
        .default_model,
    ).toBeNull();
  } finally {
    await page.close();
  }
});

test("a default changed in another window requires reopening and a second Send", async () => {
  server = await startServer({
    fixture: "tests/fixtures/claude/text-two-turns.jsonl",
    modelConnections: true,
  });
  if (!browser) throw new Error("Browser did not start");
  let listing = (await (await api("GET", "llm/connections")).json()) as ModelConnectionsListing;
  for (const suffix of ["A", "B"]) {
    listing = (await (
      await api("POST", "llm/connections", {
        expected_revision: listing.revision,
        connection: {
          name: `Synthetic connection ${suffix}`,
          model: `example/model-${suffix.toLowerCase()}`,
          base_url: "https://models.example/v1",
          api_mode: "chat_completions",
          api_key: "",
        },
      })
    ).json()) as ModelConnectionsListing;
  }
  const modelA = listing.connections.find((connection) => connection.name.endsWith(" A"));
  const modelB = listing.connections.find((connection) => connection.name.endsWith(" B"));
  if (!modelA || !modelB) throw new Error("synthetic connections were not saved");
  const settings = await (await api("GET", "settings")).json();
  await api("PATCH", "settings", {
    command_id: crypto.randomUUID(),
    expected_revision: settings.revision,
    changes: { "chat.default_model": modelA.model_ref },
  });
  const pageA = await browser.newPage();
  const pageB = await browser.newPage();
  const opens: Extract<ClientMessage, { type: "session.open" }>[] = [];
  const posts: string[] = [];
  pageA.on("websocket", (socket) => {
    socket.on("framesent", ({ payload }) => {
      const frame = JSON.parse(String(payload)) as ClientMessage;
      if (frame.type === "session.open") opens.push(frame);
    });
  });
  pageA.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/api/v2/commands"))
      posts.push(request.postData() ?? "");
  });
  const commandCount = async () =>
    ((await (await api("GET", "commands")).json()) as { commands: unknown[] }).commands.length;
  try {
    const initialPrefs = pageA
      .waitForResponse(
        (response) =>
          /\/api\/v2\/sessions\/[^/]+\/prefs$/.test(response.url()) &&
          response.request().method() === "GET",
      )
      .then((response) => ({ url: response.url(), status: response.status() }));
    const socketReady = pageA.waitForEvent("websocket");
    await pageA.goto(appUrl());
    const socket = await socketReady;
    const prefs = await initialPrefs;
    expect(prefs.status).toBe(200);
    // Browser response bodies can expire during startup; read the same canonical prefs route.
    const prefsPath = new URL(prefs.url).pathname.replace("/api/v2/", "");
    expect((await (await api("GET", prefsPath)).json()).model).toBe(modelA.model_ref);
    const sessionId = new URL(prefs.url).pathname.split("/").at(-2);
    if (!sessionId) throw new Error("draft has no session identity");
    const chip = pageA.locator('.model-chip[aria-haspopup="menu"]');
    await pageA.waitForFunction(
      (ref) =>
        document
          .querySelector('.model-chip[aria-haspopup="menu"]')
          ?.getAttribute("title")
          ?.includes(ref),
      modelA.model_ref,
    );
    expect(opens).toHaveLength(0);

    await pageB.goto(appUrl());
    await pageB.waitForSelector('[data-region="shell"]');
    await providers(pageB);
    const rowB = pageB.locator(".llm-connection").filter({ hasText: modelB.name });
    const changed = pageB.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v2/settings") && response.request().method() === "PATCH",
    );
    await rowB.getByRole("button", { name: "Use for new chats", exact: true }).click();
    expect((await changed).status()).toBe(200);
    await rowB.getByText("Default for new chats", { exact: true }).waitFor();
    expect(
      ((await (await api("GET", "llm/connections")).json()) as ModelConnectionsListing)
        .default_model,
    ).toBe(modelB.model_ref);
    expect(await chip.getAttribute("title")).toContain(modelA.model_ref);
    expect(opens).toHaveLength(0);

    const text = "synthetic two-window model consent check";
    const box = pageA.getByLabel("Message", { exact: true });
    await send(pageA, text);
    const reopen = pageA.getByRole("button", { name: "Reopen chat", exact: true });
    await reopen.waitFor();
    expect(opens).toHaveLength(1);
    expect(opens[0]).toMatchObject({ session_id: sessionId, expected_model: modelA.model_ref });
    expect(await box.inputValue()).toBe(text);
    expect(await chip.isDisabled()).toBe(true);
    expect(posts).toHaveLength(0);
    expect(await commandCount()).toBe(0);

    const synced = socket.waitForEvent("framereceived", {
      predicate: ({ payload }) => {
        const frame = JSON.parse(String(payload)) as ServerMessage;
        return frame.type === "session.synced" && frame.session_id === sessionId;
      },
    });
    await reopen.click();
    await synced;
    await pageA.waitForFunction((ref) => {
      const trigger = document.querySelector<HTMLButtonElement>(
        '.model-chip[aria-haspopup="menu"]',
      );
      return trigger && !trigger.disabled && trigger.title.includes(ref);
    }, modelB.model_ref);
    expect(opens).toHaveLength(2);
    expect(opens[1]?.session_id).toBe(sessionId);
    expect(opens[1]).not.toHaveProperty("expected_model");
    expect(await pageA.evaluate(() => sessionStorage.getItem("viki.selected"))).toBe(sessionId);
    expect(await box.inputValue()).toBe(text);
    expect(posts).toHaveLength(0);
    expect(await commandCount()).toBe(0);

    const accepted = pageA.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v2/commands") && response.request().method() === "POST",
    );
    await send(pageA, text);
    expect((await accepted).status()).toBe(202);
    await waitForText(pageA, ".reply .md", "Prime directive 1");
    expect(await chip.getAttribute("title")).toContain(modelB.model_ref);
    expect(posts).toHaveLength(1);
    expect(JSON.parse(posts[0] ?? "{}")).toMatchObject({ session_id: sessionId });
    expect(await commandCount()).toBe(1);
  } finally {
    await pageA.close();
    await pageB.close();
  }
});

test.each([800, 1024])(
  "eight long connection names fit and remain keyboard selectable at %ipx",
  async (width) => {
    server = await startServer({});
    if (!browser) throw new Error("Browser did not start");
    let listing = (await (await api("GET", "llm/connections")).json()) as ModelConnectionsListing;
    for (let index = 0; index < 8; index += 1) {
      listing = (await (
        await api("POST", "llm/connections", {
          expected_revision: listing.revision,
          connection: {
            name: `Connection ${index} ${"n".repeat(67)}`,
            model: `model-${index}-${"m".repeat(504)}`,
            base_url: "https://models.example/v1",
            api_mode: "responses",
            api_key: "",
          },
        })
      ).json()) as ModelConnectionsListing;
    }
    const first = listing.connections[0];
    const last = listing.connections[7];
    if (!first || !last) throw new Error("eight connections were not saved");
    const settings = await (await api("GET", "settings")).json();
    await api("PATCH", "settings", {
      command_id: crypto.randomUUID(),
      expected_revision: settings.revision,
      changes: { "chat.default_model": first.model_ref },
    });
    const page = await browser.newPage({ viewport: { width, height: 600 } });
    try {
      await page.goto(appUrl());
      const chip = page.locator('.model-chip[aria-haspopup="menu"]');
      await chip.waitFor();
      const triggerBox = await chip.boundingBox();
      if (!triggerBox) throw new Error("model trigger has no geometry");
      expect(triggerBox.width).toBeLessThanOrEqual(220);
      expect(triggerBox.x + triggerBox.width).toBeLessThanOrEqual(width);
      await chip.focus();
      await page.keyboard.press("ArrowDown");
      const menu = page.getByRole("menu");
      await menu.waitFor();
      await menu.locator('[role="menuitem"][data-highlighted]').waitFor();
      await page.keyboard.press("Home");
      // Include any native catalog rows; no fixture assumes their count or order.
      const modelRefs = await menu
        .locator(".mi")
        .evaluateAll((rows) => rows.map((row) => row.getAttribute("title")));
      const firstRef = modelRefs[0];
      if (!firstRef) throw new Error("model menu has no first model");
      await waitForModelFocus(page, firstRef);
      const targetIndex = modelRefs.indexOf(last.model_ref);
      expect(targetIndex).toBeGreaterThanOrEqual(0);
      for (let index = 1; index <= targetIndex; index += 1) {
        const nextRef = modelRefs[index];
        if (!nextRef) throw new Error("model menu item has no identity");
        await page.keyboard.press("ArrowDown");
        await waitForModelFocus(page, nextRef);
      }
      const target = menu.getByTitle(last.model_ref, { exact: true });
      expect(await target.getAttribute("data-highlighted")).not.toBeNull();
      const box = await target.boundingBox();
      const menuBox = await menu.boundingBox();
      if (!box || !menuBox) throw new Error("model menu has no geometry");
      expect(menuBox.x).toBeGreaterThanOrEqual(0);
      expect(menuBox.x + menuBox.width).toBeLessThanOrEqual(width);
      expect(menuBox.y).toBeGreaterThanOrEqual(0);
      expect(menuBox.y + menuBox.height).toBeLessThanOrEqual(600);
      expect(box.y).toBeGreaterThanOrEqual(menuBox.y);
      expect(box.y + box.height).toBeLessThanOrEqual(menuBox.y + menuBox.height);
      await page.keyboard.press("Enter");
      await menu.waitFor({ state: "detached" });
      await page.waitForFunction(
        (ref) =>
          document
            .querySelector('.model-chip[aria-haspopup="menu"]')
            ?.getAttribute("title")
            ?.includes(ref),
        last.model_ref,
      );
      expect(await chip.getAttribute("title")).toContain(last.model_ref);
    } finally {
      await page.close();
    }
  },
);
