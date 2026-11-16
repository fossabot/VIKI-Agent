import type { Browser } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, TEST_TOKEN } from "../../harness";
import { ORIGIN, type Server, startServer } from "../../server";

let server: Server | undefined;
let browser: Browser | undefined;
beforeAll(async () => {
  server = await startServer({
    bots: true,
    fixture: "tests/fixtures/claude/bot-thread-pass.jsonl",
  });
  browser = await launchEdge();
}, 60_000);
afterAll(async () => {
  await browser?.close();
  await server?.stop();
});

test("a worker hands completion to a fresh Checker through the native replay path", async () => {
  if (!browser) throw new Error("Browser did not launch");
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const submittedSessions: string[] = [];
  const prepared = new Map<
    string,
    { profile_id: string; workspace_id: string; bot_id: string | undefined }
  >();
  page.on("request", (request) => {
    if (request.method() !== "POST") return;
    if (request.url().endsWith("/api/v2/commands")) {
      const body = request.postDataJSON() as { session_id: string };
      submittedSessions.push(body.session_id);
    } else if (request.url().endsWith("/start-preview")) {
      const body = request.postDataJSON() as {
        session_id: string;
        workspace_id: string;
        bot_id?: string;
      };
      prepared.set(body.session_id, {
        profile_id: new URL(request.url()).pathname.split("/").at(-2) ?? "",
        workspace_id: body.workspace_id,
        bot_id: body.bot_id,
      });
    }
  });
  try {
    await page.goto(appUrl());
    const rail = page.getByRole("navigation", { name: "Navigation and sessions" });
    await rail.getByRole("button", { name: "Bot threads", exact: true }).click();
    await page.getByRole("checkbox", { name: "Chief", exact: true }).waitFor();
    expect(await page.getByRole("checkbox", { name: "Checker", exact: true }).isChecked()).toBe(
      true,
    );
    await page.getByRole("button", { name: "Create thread", exact: true }).click();
    await page
      .getByRole("textbox", { name: "Message the thread", exact: true })
      .fill("Review the example");
    await page.getByRole("button", { name: "Send to team", exact: true }).click();
    await page
      .getByText("Checker reports PASS with criteria, artifact references and evidence.", {
        exact: true,
      })
      .waitFor();
    expect(await page.locator(".bot-thread-log .reply-name").allTextContents()).toEqual([
      "Chief",
      "Checker",
    ]);
    expect(await page.locator(".bot-thread-log .reply").allTextContents()).toEqual([
      expect.stringContaining("Example reviewed."),
      expect.stringContaining("Example reviewed."),
    ]);
    expect(submittedSessions).toHaveLength(2);
    expect(new Set(submittedSessions).size).toBe(2);
    const botsResponse = await fetch(`${ORIGIN}/api/v2/bots`, {
      headers: { authorization: `Bearer ${TEST_TOKEN}` },
    });
    expect(botsResponse.ok).toBe(true);
    const { bots } = (await botsResponse.json()) as {
      bots: {
        bot_id: string;
        profile_id: string;
        workspace_id: string;
        profile: { identity: { name: string } };
      }[];
    };
    for (const [index, name] of ["Chief", "Checker"].entries()) {
      const source = prepared.get(submittedSessions[index] ?? "");
      const participant = bots.find(
        (row) => row.profile_id === source?.profile_id && row.workspace_id === source?.workspace_id,
      );
      expect(participant?.profile.identity.name).toBe(name);
      expect(source?.bot_id).toBe(participant?.bot_id);
    }
    const response = await fetch(`${ORIGIN}/api/v2/commands?limit=10`, {
      headers: { authorization: `Bearer ${TEST_TOKEN}` },
    });
    expect(response.ok).toBe(true);
    const listed = (await response.json()) as { commands: Array<{ session_id: string }> };
    expect(listed.commands.map((row) => row.session_id).sort()).toEqual(
      [...submittedSessions].sort(),
    );
    expect(await page.getByRole("button", { name: "Stop thread", exact: true }).count()).toBe(0);
  } finally {
    await page.close();
  }
});
