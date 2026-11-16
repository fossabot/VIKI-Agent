/** The real bundle and scratch backend keep optional messaging behind explicit controls. */
import type { Browser, Page } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import {
  MESSAGING_PROVIDERS,
  type MessagingConfig,
  type MessagingField,
  type MessagingProvider,
  type MessagingState,
  type MessagingStatus,
} from "../../../src/clients/messaging/messagingApi";
import { appUrl, launchEdge, TEST_TOKEN } from "../../harness";
import { ORIGIN, PORT, type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

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

function status(state: MessagingState = "disabled"): MessagingStatus {
  const entry = {
    configured: state !== "not_configured",
    state,
    restart_required: false,
    reason: null,
  };
  return { mattermost: { ...entry }, slack: { ...entry }, discord: { ...entry } };
}

async function providerLogos(page: Page) {
  const messaging = page.getByRole("region", { name: "Messaging", exact: true });
  expect(await messaging.locator("img").count()).toBe(3);
  for (const name of ["Mattermost", "Slack", "Discord"]) {
    const card = messaging.getByRole("region", { name: `${name} bot`, exact: true });
    expect(await card.getByRole("heading", { name: `${name} bot`, exact: true }).isVisible()).toBe(
      true,
    );
    const image = card.locator("img");
    expect(await image.count()).toBe(1);
    await image.evaluate((node) => (node as HTMLImageElement).decode());
    const decoded = await image.evaluate((node) => {
      const image = node as HTMLImageElement;
      const rect = image.getBoundingClientRect();
      return {
        complete: image.complete,
        naturalWidth: image.naturalWidth,
        width: rect.width,
        height: rect.height,
        alt: image.alt,
        hidden: image.getAttribute("aria-hidden"),
        local:
          image.currentSrc.startsWith("data:image/svg+xml") ||
          image.currentSrc.startsWith("data:image/png") ||
          new URL(image.currentSrc).origin === window.location.origin,
      };
    });
    expect(decoded.complete).toBe(true);
    expect(decoded.naturalWidth).toBeGreaterThan(0);
    expect(decoded.width).toBe(32);
    expect(decoded.height).toBe(32);
    expect(decoded.alt).toBe("");
    expect(decoded.hidden).toBe("true");
    expect(decoded.local).toBe(true);
    expect(await card.getByRole("img").count()).toBe(0);
  }
}

async function forbidRemote(page: Page) {
  const escaped: string[] = [];
  const sends: string[] = [];
  await page.route("**/*", async (route) => {
    if (new URL(route.request().url()).origin === ORIGIN) await route.continue();
    else {
      escaped.push("http");
      await route.abort();
    }
  });
  page.on("websocket", (socket) => {
    if (new URL(socket.url()).host !== new URL(ORIGIN).host) escaped.push("websocket");
    socket.on("framesent", ({ payload }) => {
      const frame = JSON.parse(String(payload)) as { type?: string };
      if (frame.type === "turn.send") sends.push(frame.type);
    });
  });
  return { escaped, sends };
}

interface Reply {
  status: number;
  json:
    | MessagingStatus
    | { error: { code: string; message: string; retry_after_seconds?: number } };
}

/** Synthetic saved values the `config` fixture answers with: real-shaped but inert. */
const CONFIG_VALUES: Record<MessagingProvider, Partial<Record<MessagingField, string>>> = {
  mattermost: { url: "https://mattermost.example.test", owner_id: "mattermost-owner-id" },
  slack: { owner_id: "slack-owner-id" },
  discord: { owner_id: "discord-owner-id", client_url: "https://discord.example.test" },
};

/** Only local routes are replaced; the browser still authenticates through the real client. */
async function fixture(page: Page) {
  const calls: string[] = [];
  const answers = new Map<string, { started: () => void; answer: Promise<Reply> }>();
  const current = status();
  await page.route(`${ORIGIN}/api/v2/messaging{,/**}`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    expect(request.headers().authorization).toBe(`Bearer ${TEST_TOKEN}`);
    expect(url.search).toBe("");
    expect(request.postData()).toBeNull();
    const key = url.pathname.slice("/api/v2/messaging".length).replace(/^\//, "") || "get";
    // Settings are read alongside status on every mount and refresh; answer with all three
    // providers already saved, before the POST-oriented checks below, and leave `calls`
    // untouched so the existing exact-sequence assertions do not need to name it.
    if (key === "config") {
      expect(request.method()).toBe("GET");
      const json: MessagingConfig = {
        mattermost: { source: "saved", values: CONFIG_VALUES.mattermost },
        slack: { source: "saved", values: CONFIG_VALUES.slack },
        discord: { source: "saved", values: CONFIG_VALUES.discord },
      };
      await route.fulfill({ status: 200, json, headers: { "Cache-Control": "no-store" } });
      return;
    }
    expect(request.method()).toBe(key === "get" ? "GET" : "POST");
    const [provider, action] = key.split("/");
    if (key !== "get") {
      expect(MESSAGING_PROVIDERS).toContain(provider);
      expect(["enable", "disable"]).toContain(action);
      expect(request.headers().origin).toBe(ORIGIN);
    }
    calls.push(key);
    const planned = answers.get(key);
    answers.delete(key);
    let reply: Reply;
    if (planned) {
      planned.started();
      reply = await planned.answer;
    } else {
      if (key !== "get") {
        current[provider as MessagingProvider] = {
          configured: true,
          state: action === "enable" ? "connected" : "disabled",
          restart_required: false,
          reason: null,
        };
      }
      reply = { status: 200, json: structuredClone(current) };
    }
    await route.fulfill({ ...reply, headers: { "Cache-Control": "no-store" } });
  });
  return {
    calls,
    snapshot: () => structuredClone(current),
    set(provider: MessagingProvider, state: MessagingState, restartRequired = false) {
      current[provider] = {
        configured: true,
        state,
        restart_required: restartRequired,
        reason: null,
      };
    },
    hold(key: string) {
      let started!: () => void;
      let release!: (reply: Reply) => void;
      const waiting = new Promise<void>((resolve) => {
        started = resolve;
      });
      const answer = new Promise<Reply>((resolve) => {
        release = resolve;
      });
      answers.set(key, { started, answer });
      return { started: waiting, release };
    },
  };
}

test("Messaging shows real unconfigured status, safe setup and existing Capabilities navigation", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  try {
    const privacy = await forbidRemote(page);
    await page.goto(appUrl(PORT));
    await page.locator('[data-region="shell"]').waitFor();
    const statusRead = page.waitForEvent(
      "requestfinished",
      (request) => new URL(request.url()).pathname === "/api/v2/messaging",
    );
    await navigateSidebar(page, "Messaging");
    const response = await (await statusRead).response();
    if (!response) throw new Error("the Messaging status read has no response");
    expect(response.status()).toBe(200);
    expect(response.request().headers().authorization).toBe(`Bearer ${TEST_TOKEN}`);
    expect(response.headers()["cache-control"]).toBe("no-store");
    expect(await response.json()).toEqual(status("not_configured"));
    const messaging = page.getByRole("region", { name: "Messaging", exact: true });
    await providerLogos(page);
    for (const name of ["Mattermost", "Slack", "Discord"]) {
      const card = messaging.getByRole("region", { name: `${name} bot`, exact: true });
      await card.getByText("Not configured", { exact: true }).waitFor();
      expect(await card.getByRole("switch").count()).toBe(0);
      expect(
        await card.getByRole("button", { name: `Set up ${name}`, exact: true }).isVisible(),
      ).toBe(true);
      expect(
        await card
          .getByRole("button", { name: `${name} bot`, exact: true })
          .getAttribute("aria-expanded"),
      ).toBe("false");
    }
    const mattermost = messaging.getByRole("region", { name: "Mattermost bot", exact: true });
    await mattermost.getByRole("button", { name: "Set up Mattermost", exact: true }).click();
    const serverUrl = mattermost.getByRole("textbox", { name: "Server URL", exact: true });
    await serverUrl.waitFor();
    expect(await serverUrl.evaluate((node) => node.matches(":focus"))).toBe(true);
    expect(await mattermost.getByText("Bot token", { exact: true }).isVisible()).toBe(true);
    await mattermost.locator("summary", { hasText: "Setup steps" }).click();
    expect(await mattermost.getByText("!viki help", { exact: true }).isVisible()).toBe(true);
    await messaging.getByRole("button", { name: "Open Capabilities", exact: true }).click();
    await page.getByRole("region", { name: "Capabilities", exact: true }).waitFor();
    expect(await page.getByRole("tab", { name: "Plugins", exact: true }).isVisible()).toBe(true);
    expect(privacy).toEqual({ escaped: [], sends: [] });
  } finally {
    await page.close();
  }
});

test("independent toggles retain newer actions across crossed replies and stale Refresh", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  try {
    const privacy = await forbidRemote(page);
    const wire = await fixture(page);
    await page.goto(appUrl(PORT));
    await navigateSidebar(page, "Messaging");
    const messaging = page.getByRole("region", { name: "Messaging", exact: true });
    const slack = messaging.getByRole("region", { name: "Slack bot", exact: true });
    const discord = messaging.getByRole("region", { name: "Discord bot", exact: true });
    const mattermost = messaging.getByRole("region", { name: "Mattermost bot", exact: true });
    await slack.getByText("Disabled", { exact: true }).waitFor();
    await providerLogos(page);
    for (const name of ["Mattermost", "Slack", "Discord"])
      expect(
        await messaging
          .getByRole("region", { name: `${name} bot`, exact: true })
          .getByText("Disabled", { exact: true })
          .isVisible(),
      ).toBe(true);
    expect(wire.calls).toEqual(["get"]);
    expect(privacy).toEqual({ escaped: [], sends: [] });

    const slackSwitch = slack.getByRole("switch", { name: "Slack bot", exact: true });
    const discordSwitch = discord.getByRole("switch", { name: "Discord bot", exact: true });
    const first = wire.hold("slack/enable");
    const second = wire.hold("discord/enable");
    expect(await slackSwitch.getAttribute("aria-checked")).toBe("false");
    await slackSwitch.click();
    await first.started;
    expect(await discordSwitch.getAttribute("aria-checked")).toBe("false");
    await discordSwitch.click();
    await second.started;
    wire.set("discord", "connected");
    second.release({ status: 200, json: wire.snapshot() });
    await discord.getByText("Connected", { exact: true }).waitFor();
    expect(await slack.getByText("Enabling…", { exact: true }).isVisible()).toBe(true);
    const crossed = status();
    crossed.slack.state = "connected";
    wire.set("slack", "connected");
    first.release({ status: 200, json: crossed });
    await slack.getByText("Connected", { exact: true }).waitFor();
    expect(await discord.getByText("Connected", { exact: true }).isVisible()).toBe(true);
    expect(await mattermost.getByText("Disabled", { exact: true }).isVisible()).toBe(true);

    const stale = wire.snapshot();
    const refresh = wire.hold("get");
    await messaging.getByRole("button", { name: "Refresh status", exact: true }).click();
    await refresh.started;
    expect(await slackSwitch.getAttribute("aria-checked")).toBe("true");
    await slackSwitch.click();
    await slack.getByText("Disabled", { exact: true }).waitFor();
    refresh.release({ status: 200, json: stale });
    await page.waitForFunction(
      () =>
        !document.querySelector<HTMLButtonElement>(".messaging-view .page-actions button")
          ?.disabled,
    );
    expect(await slack.getByText("Disabled", { exact: true }).isVisible()).toBe(true);
    expect(await discord.getByText("Connected", { exact: true }).isVisible()).toBe(true);
    expect(await slackSwitch.getAttribute("aria-checked")).toBe("false");
    await slackSwitch.click();
    await slack.getByText("Connected", { exact: true }).waitFor();
    expect(await slackSwitch.getAttribute("aria-checked")).toBe("true");
    await slackSwitch.click();
    await slack.getByText("Disabled", { exact: true }).waitFor();
    // Polls add GETs while a bot is connected; only the POST order is exact.
    expect(wire.calls[0]).toBe("get");
    expect(wire.calls.filter((call) => call === "get").length).toBeGreaterThanOrEqual(2);
    expect(wire.calls.filter((call) => call !== "get")).toEqual([
      "slack/enable",
      "discord/enable",
      "slack/disable",
      "slack/enable",
      "slack/disable",
    ]);
    expect(privacy).toEqual({ escaped: [], sends: [] });
  } finally {
    await page.close();
  }
});

test("stopped bots connect explicitly and failures or cooldown do not affect siblings", async () => {
  if (!browser) throw new Error("Edge did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  try {
    const privacy = await forbidRemote(page);
    const wire = await fixture(page);
    wire.set("discord", "stopped");
    await page.goto(appUrl(PORT));
    await navigateSidebar(page, "Messaging");
    const messaging = page.getByRole("region", { name: "Messaging", exact: true });
    const slack = messaging.getByRole("region", { name: "Slack bot", exact: true });
    const discord = messaging.getByRole("region", { name: "Discord bot", exact: true });
    await discord.getByText("Stopped", { exact: true }).waitFor();
    expect(wire.calls).toEqual(["get"]);
    const slackSwitch = slack.getByRole("switch", { name: "Slack bot", exact: true });
    const discordSwitch = discord.getByRole("switch", { name: "Discord bot", exact: true });
    expect(await discordSwitch.getAttribute("aria-checked")).toBe("false");
    await discordSwitch.click();
    await discord.getByText("Connected", { exact: true }).waitFor();

    const failure = wire.hold("slack/enable");
    expect(await slackSwitch.getAttribute("aria-checked")).toBe("false");
    await slackSwitch.click();
    await failure.started;
    failure.release({
      status: 503,
      json: { error: { code: "unavailable", message: "synthetic server detail" } },
    });
    await slack.getByRole("alert").waitFor();
    expect(await slack.getByRole("alert").textContent()).toBe(
      "Couldn't connect the Slack bot. Refresh status or try again.",
    );
    expect(await messaging.getByText("synthetic server detail").count()).toBe(0);
    expect(await discord.getByText("Connected", { exact: true }).isVisible()).toBe(true);

    const limited = wire.hold("slack/enable");
    expect(await slackSwitch.getAttribute("aria-checked")).toBe("false");
    await slackSwitch.click();
    await limited.started;
    limited.release({
      status: 429,
      json: {
        error: {
          code: "cooldown",
          message: "Wait before connecting again.",
          retry_after_seconds: 60,
        },
      },
    });
    await slack.getByText(/Wait up to \d+ seconds before connecting again\./).waitFor();
    expect(await slackSwitch.getAttribute("aria-disabled")).toBe("true");
    expect(await discordSwitch.getAttribute("aria-disabled")).toBeNull();
    expect(await discordSwitch.getAttribute("aria-checked")).toBe("true");
    wire.set("slack", "stopped", true);
    await messaging.getByRole("button", { name: "Refresh status", exact: true }).click();
    const restartNote = slack.getByText(
      "Restart VIKI Agent to use this bot again with the current workspace.",
    );
    await restartNote.waitFor();
    const restartId = await restartNote.getAttribute("id");
    const describedBy = (await slackSwitch.getAttribute("aria-describedby")) ?? "";
    expect(await slackSwitch.getAttribute("aria-disabled")).toBe("true");
    expect(` ${describedBy} `.includes(` ${restartId} `)).toBe(true);
    // Polls add GETs while a bot is connected; only the POST order is exact.
    expect(wire.calls[0]).toBe("get");
    expect(wire.calls.filter((call) => call === "get").length).toBeGreaterThanOrEqual(2);
    expect(wire.calls.filter((call) => call !== "get")).toEqual([
      "discord/enable",
      "slack/enable",
      "slack/enable",
    ]);
    expect(privacy).toEqual({ escaped: [], sends: [] });
  } finally {
    await page.close();
  }
});
