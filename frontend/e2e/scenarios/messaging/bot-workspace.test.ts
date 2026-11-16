import type { Browser } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge } from "../../harness";
import { type Server, startServer } from "../../server";

let server: Server | undefined;
let browser: Browser | undefined;
beforeAll(async () => {
  server = await startServer({ bots: true, fixture: "tests/fixtures/claude/text-two-turns.jsonl" });
  browser = await launchEdge();
}, 60_000);
afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

test("Bots search keeps its size beside Chief, faces fill the rail, and Kanban has status lanes", async () => {
  if (!browser) throw new Error("Browser did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  try {
    await page.goto(appUrl());
    const rail = page.getByRole("navigation", { name: "Navigation and sessions" });
    await page.locator(".rail-team-member .bot-face").first().waitFor();
    expect(
      await page
        .locator(".rail-team-member .bot-face")
        .first()
        .evaluate((node) => node.getBoundingClientRect().width),
    ).toBe(56);
    await rail.getByRole("button", { name: "Bots", exact: true }).click();
    const search = page.getByRole("searchbox", { name: "Find a Bot" });
    await page.getByText("Not started", { exact: true }).first().waitFor();
    const before = await search.boundingBox();
    await page.getByRole("button", { name: "Chief", exact: true }).click();
    await page.getByRole("region", { name: "Selected Bot" }).waitFor();
    const after = await search.boundingBox();
    expect(before?.height).toBe(40);
    expect(after).toEqual(before);
    const tops = await page
      .locator(".bots-body")
      .evaluate((node) => [...node.children].map((child) => child.getBoundingClientRect().top));
    expect(tops[0]).toBe(tops[1]);
    expect(await rail.getByRole("button", { name: "Agents", exact: true }).count()).toBe(0);
    await page.getByRole("button", { name: "Checker", exact: true }).click();
    await page.getByRole("button", { name: "Open conversation", exact: true }).waitFor();
    await page.getByRole("button", { name: "Bot settings", exact: true }).click();
    await page.getByRole("heading", { name: "Bot settings", exact: true }).waitFor();
    await rail.getByRole("button", { name: "Kanban", exact: true }).click();
    await page.getByRole("heading", { name: "To do (0)" }).waitFor();
    const laneBoxes = () =>
      page.locator(".kanban-lane").evaluateAll((nodes) =>
        nodes.map((node) => {
          const box = node.getBoundingClientRect();
          return { x: box.x, top: box.top, width: box.width };
        }),
      );
    const lanes = await laneBoxes();
    expect(lanes).toHaveLength(5);
    expect(new Set(lanes.map((lane) => lane.top)).size).toBe(1);
    expect(
      lanes.every((lane, i) => lane.width >= 200 && (!i || lane.x > (lanes[i - 1]?.x ?? 0))),
    ).toBe(true);
    expect(
      await page.locator(".kanban-lanes").evaluate((node) => node.scrollWidth <= node.clientWidth),
    ).toBe(true);
    for (const width of [900, 620]) {
      await page.setViewportSize({ width, height: 900 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
        true,
      );
    }
    expect(new Set((await laneBoxes()).map((lane) => lane.x)).size).toBe(1);
  } finally {
    await page.close();
  }
});

test("a standalone thread starts a core Bot without settings and shows the real reply", async () => {
  if (!browser) throw new Error("Browser did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  try {
    await page.goto(appUrl());
    const rail = page.getByRole("navigation", { name: "Navigation and sessions" });
    await rail.getByRole("button", { name: "Bot threads", exact: true }).click();
    await page.getByRole("checkbox", { name: "Chief", exact: true }).waitFor();
    await page.getByRole("checkbox", { name: "Research", exact: true }).check();
    await page.getByRole("button", { name: "Create thread", exact: true }).click();
    await page
      .getByRole("textbox", { name: "Message the thread", exact: true })
      .fill("Review this example.");
    await page.getByRole("button", { name: "Send to team", exact: true }).click();
    await page.locator(".bot-thread-log .reply").waitFor();
    await expect
      .poll(() => page.locator(".bot-thread-log .reply-name").first().textContent())
      .toBe("Chief");
    await page
      .getByText("Waiting for your message. The Bot did not request another handoff.", {
        exact: true,
      })
      .waitFor();
    expect(await page.locator(".bot-thread-log .reply").count()).toBe(1);
    expect(await page.locator('[data-region="right-rail"]').count()).toBe(0);
    await page.getByRole("button", { name: "Hide participants", exact: true }).click();
    expect(await page.getByRole("complementary", { name: "Participants" }).count()).toBe(0);
    await page.getByRole("button", { name: "Show participants", exact: true }).click();
    await page.getByRole("checkbox", { name: "Research", exact: true }).uncheck();
    expect(await page.getByRole("checkbox", { name: "Research", exact: true }).isChecked()).toBe(
      false,
    );
    await page
      .getByRole("textbox", { name: "Message the thread", exact: true })
      .fill("Keep this unsent message.");
    await rail.getByRole("button", { name: "Bots", exact: true }).click();
    await rail.getByRole("button", { name: "Bot threads", exact: true }).click();
    expect(
      await page.getByRole("textbox", { name: "Message the thread", exact: true }).inputValue(),
    ).toBe("Keep this unsent message.");
    expect(await page.locator(".bot-thread-log .reply").count()).toBe(1);
    await page.reload();
    await page
      .getByText(
        "Some source messages are unavailable. Reload saved threads to try again, or open the individual Bot chat to review its history.",
        { exact: true },
      )
      .waitFor();
    expect(await page.locator(".bot-thread-log > li").count()).toBe(2);
    expect(await page.locator(".bot-thread-user-text").textContent()).toBe("Review this example.");
    expect(await page.getByRole("button", { name: "Send to team", exact: true }).isDisabled()).toBe(
      true,
    );
    expect(await page.getByRole("checkbox", { name: "Research", exact: true }).isChecked()).toBe(
      false,
    );
    expect(
      await page.getByRole("textbox", { name: "Message the thread", exact: true }).inputValue(),
    ).toBe("");
    expect(await page.getByRole("button", { name: "Stop thread", exact: true }).count()).toBe(0);
    await page.getByRole("button", { name: "Delete thread group…", exact: true }).click();
    const dialog = page.getByRole("alertdialog", { name: "Delete “Review this example.”?" });
    await dialog.getByRole("button", { name: "Delete thread group", exact: true }).click();
    await page.getByText("Deleted “Review this example.”.", { exact: true }).waitFor();
  } finally {
    await page.close();
  }
});
