import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { createFakeDecisionLoopApi, decisionLoopExamples } from "../../clients/decisionLoop/fake";
import type { DecisionLoopApi, PageView } from "../../clients/decisionLoop/types";
import { t } from "../../i18n/t";
import { readCss } from "../../testing/css";
import { DecisionLoopPage, type DecisionLoopPageProps } from "./DecisionLoopPage";

afterEach(cleanup);

const click = async (element: HTMLElement) => act(async () => void fireEvent.click(element));

/** jsdom has no `scrollIntoView`: stand one in while `run` runs and answer the ids it scrolled. */
async function scrolledDuring(run: () => Promise<void>): Promise<string[]> {
  const scrolled: string[] = [];
  const prototype = window.HTMLElement.prototype as unknown as {
    scrollIntoView: ((this: HTMLElement) => void) | undefined;
  };
  const original = prototype.scrollIntoView;
  prototype.scrollIntoView = function scrollIntoViewStub(this: HTMLElement) {
    scrolled.push(this.id);
  };
  try {
    await run();
    await act(async () => {}); // flush the page's scroll effect before the stand-in goes
  } finally {
    prototype.scrollIntoView = original;
  }
  return scrolled;
}

function openBodies(): string[] {
  return ROW_BODY_IDS.filter((id) => document.getElementById(`${id}-body`)?.hidden === false);
}

function baseProps(overrides: Partial<DecisionLoopPageProps> = {}): DecisionLoopPageProps {
  return {
    api: createFakeDecisionLoopApi(),
    values: {},
    writable: true,
    patch: vi.fn(async () => undefined),
    onOpenUrl: vi.fn(),
    onOpenContext: vi.fn(),
    onOpenChat: vi.fn(),
    ...overrides,
  };
}

/** The page's own row order, with Learning between How VIKI is doing and Data, read off each
 * row's own disclosure name. The card and the How VIKI works strip are not rows. */
const ROW_NAMES = [
  "What VIKI watches",
  "Decision model",
  "What leaves this computer",
  "How VIKI may reach you",
  "What VIKI may do",
  "How VIKI is doing",
  "Learning",
  "Data",
] as const;

const ROW_BODY_IDS = ["watch", "model", "privacy", "reach", "do", "how", "learning", "data"];
const GROUP_NAMES = [
  "What VIKI notices",
  "How VIKI decides",
  "How VIKI tells you",
  "Work VIKI can start",
  "History and learning",
];

async function openAllRows(): Promise<void> {
  for (const name of ROW_NAMES) {
    await click(screen.getByRole("button", { name }));
  }
}

test("shows the loading kit while the first page is in flight, then the heading and rows", async () => {
  render(<DecisionLoopPage {...baseProps()} />);
  expect(screen.getByRole("heading", { name: "Decision" })).toBeTruthy();
  expect(screen.getByText("Loading Decision…")).toBeTruthy();
  await waitFor(() => expect(screen.queryByText("Loading Decision…")).toBeNull());
  expect(screen.getByRole("region", { name: "Proactive VIKI" })).toBeTruthy();
});

test("renders the eight rows in order, Learning between How VIKI is doing and Data", async () => {
  const { container } = render(<DecisionLoopPage {...baseProps()} />);
  await waitFor(() => expect(screen.queryByText("Loading Decision…")).toBeNull());
  // Scoped to each row's own disclosure (`DecisionRow.tsx`'s `.dloop-disclosure`), not a bare
  // `button[aria-expanded]`: the Learning row's own "Show all numbers" toggle carries
  // aria-expanded too, and its body stays mounted — just `hidden` — while the row is closed.
  const disclosures = Array.from(
    container.querySelectorAll<HTMLButtonElement>(".dloop-disclosure[aria-expanded]"),
  );
  expect(disclosures.map((button) => button.textContent)).toEqual([...ROW_NAMES]);
});

test("a failed page load shows 'Decision is unavailable right now.' with Retry; Retry re-fetches", async () => {
  const api = createFakeDecisionLoopApi();
  api.fail("page", "unavailable");
  render(<DecisionLoopPage {...baseProps({ api })} />);
  await screen.findByText("Decision is unavailable right now.");
  expect(screen.getByRole("alert").textContent).toBe("Decision is unavailable right now.");
  expect(screen.queryByRole("region", { name: "Proactive VIKI" })).toBeNull();
  expect(screen.getByRole("heading", { level: 1, name: "Decision" })).toBeTruthy();

  await click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(screen.getByRole("region", { name: "Proactive VIKI" })).toBeTruthy());
  expect(screen.queryByText("Decision is unavailable right now.")).toBeNull();
});

test("the card offers Turn proactive VIKI on while decision.enabled is false", async () => {
  const page: PageView = { ...decisionLoopExamples().page, enabled: false };
  const api = createFakeDecisionLoopApi({ page });
  render(<DecisionLoopPage {...baseProps({ api })} />);
  const card = await screen.findByRole("region", { name: "Proactive VIKI" });
  expect(within(card).getByRole("button", { name: "Turn proactive VIKI on" })).toBeTruthy();
  expect(within(card).queryByRole("switch")).toBeNull();
});

test("the How VIKI works strip is a region while off and a closed details while on", async () => {
  const page: PageView = { ...decisionLoopExamples().page, enabled: false };
  const api = createFakeDecisionLoopApi({ page });
  const off = render(<DecisionLoopPage {...baseProps({ api })} />);
  await screen.findByRole("region", { name: "Proactive VIKI" });
  expect(screen.getByRole("region", { name: "How VIKI works" })).toBeTruthy();
  expect(off.container.querySelector("details.dloop-how")).toBeNull();
  cleanup();

  const on = render(<DecisionLoopPage {...baseProps()} />);
  await screen.findByRole("region", { name: "Proactive VIKI" });
  const strip = on.container.querySelector<HTMLDetailsElement>("details.dloop-how");
  expect(strip?.open).toBe(false);
  expect(strip?.querySelector("summary")?.textContent).toBe("How VIKI works");
  expect(screen.queryByRole("region", { name: "How VIKI works" })).toBeNull();
});

test("the page head, the card and the strip come first, then five groups in order", async () => {
  const { container } = render(<DecisionLoopPage {...baseProps()} />);
  const card = await screen.findByRole("region", { name: "Proactive VIKI" });
  const head = container.querySelector("header.page-head");
  expect(head?.querySelector("h1.page-title")?.textContent).toBe("Decision");
  expect(head?.querySelector("p.page-intro")?.textContent).toBe(t("decision_page.intro"));

  const groups = Array.from(container.querySelectorAll<HTMLElement>("section.dloop-group"));
  const titles = groups.map((group) => group.querySelector("h2.dloop-group-title"));
  GROUP_NAMES.forEach((name, i) => {
    expect(screen.getByRole("heading", { level: 2, name })).toBe(titles[i]);
  });
  expect(titles.map((title) => title?.id)).toEqual([
    "decision-notices",
    "decision-decides",
    "decision-tells",
    "decision-actions",
    "decision-history",
  ]);
  expect(
    Array.from(container.querySelectorAll(".dloop-step-no")).map((no) => [
      no.textContent,
      no.getAttribute("aria-hidden"),
    ]),
  ).toEqual([
    ["1", "true"],
    ["2", "true"],
    ["3", "true"],
    ["4", "true"],
  ]);
  const rowsIn = (group: HTMLElement) =>
    Array.from(group.querySelectorAll(".dloop-disclosure")).map((button) => button.textContent);
  expect(groups.map(rowsIn)).toEqual([
    ["What VIKI watches"],
    ["Decision model", "What leaves this computer"],
    ["How VIKI may reach you"],
    ["What VIKI may do"],
    ["How VIKI is doing", "Learning", "Data"],
  ]);
  const strip = container.querySelector<HTMLElement>(".dloop-how");
  expect(head?.compareDocumentPosition(card)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  expect(card.compareDocumentPosition(strip as HTMLElement)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  expect(strip?.compareDocumentPosition(groups[0] as HTMLElement)).toBe(
    Node.DOCUMENT_POSITION_FOLLOWING,
  );
});

test("one read-only notice shows while settings cannot change, and none inside a row", async () => {
  const { container } = render(<DecisionLoopPage {...baseProps({ writable: false })} />);
  await screen.findByRole("region", { name: "Proactive VIKI" });
  expect(screen.getAllByText(t("decision_page.read_only"))).toHaveLength(1);
  expect(container.querySelectorAll(".dloop-page > p.dloop-hint")).toHaveLength(1);
  cleanup();
  render(<DecisionLoopPage {...baseProps()} />);
  await screen.findByRole("region", { name: "Proactive VIKI" });
  expect(screen.queryByText(t("decision_page.read_only"))).toBeNull();
});

test("the page has no Help button", async () => {
  render(<DecisionLoopPage {...baseProps()} />);
  await screen.findByRole("region", { name: "Proactive VIKI" });
  expect(screen.queryByRole("button", { name: /help/i })).toBeNull();
});

test("a How VIKI works button opens its row, scrolls its heading and focuses its disclosure", async () => {
  const scrolled = await scrolledDuring(async () => {
    render(<DecisionLoopPage {...baseProps()} />);
    await screen.findByRole("region", { name: "Proactive VIKI" });
    expect(openBodies()).toEqual([]);
    await click(screen.getByRole("button", { name: "Choose what it watches" }));
    expect(openBodies()).toEqual(["watch"]);
    expect(document.activeElement).toBe(document.querySelector("#watch-heading .dloop-disclosure"));
    // The Your data line names what leaves this computer, which is the privacy row.
    await click(screen.getByRole("button", { name: "See what leaves this computer" }));
  });
  expect(openBodies()).toEqual(["watch", "privacy"]);
  expect(scrolled).toEqual(["watch-heading", "privacy-heading"]);
});

test("the key field never shows a saved key, before or after Replace", async () => {
  render(<DecisionLoopPage {...baseProps()} />);
  await waitFor(() => expect(screen.queryByText("Loading Decision…")).toBeNull());
  await click(screen.getByRole("button", { name: "Decision model" }));
  // examples.json's model.key is "saved"; the field is hidden until Replace is pressed.
  expect(screen.queryByLabelText("Key")).toBeNull();
  await click(screen.getByRole("button", { name: "Replace" }));
  expect((screen.getByLabelText("Key") as HTMLInputElement).value).toBe("");
});

test("Delete all decision history names the exact stored count", async () => {
  render(<DecisionLoopPage {...baseProps()} />);
  await waitFor(() => expect(screen.queryByText("Loading Decision…")).toBeNull());
  await click(screen.getByRole("button", { name: "Data" }));
  await screen.findByText("Decision history");
  await click(screen.getByRole("button", { name: "Delete all decision history" }));
  expect(screen.getByRole("alertdialog").textContent).toContain("All 1284 decisions and reports.");
});

test("See every decision in Data opens Data and focuses its heading", async () => {
  const scrolled = await scrolledDuring(async () => {
    render(<DecisionLoopPage {...baseProps()} />);
    await waitFor(() => expect(screen.queryByText("Loading Decision…")).toBeNull());
    await click(screen.getByRole("button", { name: "How VIKI is doing" }));
    await click(screen.getByRole("button", { name: "See every decision in Data" }));
    await screen.findByText("Decision history");
    expect(openBodies()).toContain("data");
    expect(document.activeElement).toBe(document.querySelector("#data-browse"));
  });
  expect(scrolled).toContain("data-heading");
});

test("every row starts closed", async () => {
  render(<DecisionLoopPage {...baseProps()} />);
  await waitFor(() => expect(screen.queryByText("Loading Decision…")).toBeNull());
  expect(openBodies()).toEqual([]);
  for (const id of ROW_BODY_IDS) expect(document.getElementById(`${id}-body`)).toBeTruthy();
});

test("the Proactive VIKI card's write re-reads the page", async () => {
  const api = createFakeDecisionLoopApi();
  render(<DecisionLoopPage {...baseProps({ api })} />);
  await waitFor(() => expect(screen.queryByText("Loading Decision…")).toBeNull());
  expect(api.requests.filter((request) => request.method === "page")).toHaveLength(1);

  await click(screen.getByRole("switch", { name: "Proactive VIKI" }));
  expect(api.requests.filter((request) => request.method === "page")).toHaveLength(2);
});

test("saving Learning refreshes its status from the saved settings", async () => {
  let enabled = true;
  const page = decisionLoopExamples().page;
  const api = {
    ...createFakeDecisionLoopApi(),
    page: vi.fn(async () => ({ ...page, learning: { ...page.learning, enabled } })),
  };
  function Settings() {
    const [values, setValues] = useState({ "decision.learning": true });
    return (
      <DecisionLoopPage
        {...baseProps({ api, values })}
        patch={async (changes) => {
          enabled = changes["decision.learning"] === true;
          setValues({ "decision.learning": enabled });
        }}
      />
    );
  }
  render(<Settings />);
  await act(async () => {});
  expect(screen.getByRole("button", { name: "Learning" })).toBeTruthy();
  await click(screen.getByRole("switch", { name: "Learning" }));
  const row = screen.getByRole("region", { name: "Learning" });
  expect(within(row).getByRole("status").textContent).toBe("Off");
  expect(within(row).getByRole("switch").getAttribute("aria-checked")).toBe("false");
  expect(api.page).toHaveBeenCalledTimes(2);
});

test("openRow opens that row and scrolls its heading into view", async () => {
  const calls = await scrolledDuring(async () => {
    render(<DecisionLoopPage {...baseProps({ openRow: "model" })} />);
    await screen.findByRole("button", { name: "Decision model" });
  });
  expect(openBodies()).toEqual(["model"]);
  expect(calls).toEqual(["model-heading"]);
});

test("openRow proactive scrolls to the card's heading and opens nothing", async () => {
  const calls = await scrolledDuring(async () => {
    render(<DecisionLoopPage {...baseProps({ openRow: "proactive" })} />);
    await screen.findByRole("region", { name: "Proactive VIKI" });
  });
  expect(openBodies()).toEqual([]);
  expect(calls).toEqual(["proactive-heading"]);
});

test("an unknown openRow opens nothing and never throws", async () => {
  const calls = await scrolledDuring(async () => {
    render(<DecisionLoopPage {...baseProps({ openRow: "not-a-row" })} />);
    await waitFor(() => expect(screen.queryByText("Loading Decision…")).toBeNull());
  });
  expect(openBodies()).toEqual([]);
  expect(calls).toEqual([]);
});

test("aborts the in-flight page fetch on unmount", async () => {
  let capturedSignal: AbortSignal | undefined;
  const api: DecisionLoopApi = {
    ...createFakeDecisionLoopApi(),
    page: vi.fn((signal?: AbortSignal) => {
      capturedSignal = signal;
      return new Promise<PageView>(() => undefined);
    }),
  };
  const { unmount } = render(<DecisionLoopPage {...baseProps({ api })} />);
  expect(capturedSignal).toBeTruthy();
  expect(capturedSignal?.aborted).toBe(false);
  unmount();
  expect(capturedSignal?.aborted).toBe(true);
});

test("drops a stale answer that settles after a newer one (out-of-order loads)", async () => {
  const resolvers: Array<(value: PageView) => void> = [];
  let calls = 0;
  const fake = createFakeDecisionLoopApi();
  const api: DecisionLoopApi = {
    ...fake,
    page: vi.fn((signal?: AbortSignal) => {
      calls += 1;
      if (calls === 1) return fake.page(signal); // mount: resolve normally so the rows render
      return new Promise<PageView>((resolve) => resolvers.push(resolve));
    }),
  };
  const patch = vi.fn(async () => undefined);
  render(<DecisionLoopPage {...baseProps({ api, patch })} />);
  await waitFor(() => expect(screen.queryByText("Loading Decision…")).toBeNull());

  await click(screen.getByRole("switch", { name: "Proactive VIKI" })); // triggers refresh #1
  expect(resolvers).toHaveLength(1);
  await click(screen.getByRole("switch", { name: "Proactive VIKI" })); // triggers refresh #2
  expect(resolvers).toHaveLength(2);

  // role="status" takes its accessible name only from aria-label/aria-labelledby (ARIA's "name
  // from: author"), never from content, so the chip is found by its row's region and read by
  // textContent — the same pattern DoingRows.unit.test.tsx's own status-chip checks use.
  function howStatusText(): string | null {
    return within(screen.getByRole("region", { name: "How VIKI is doing" })).getByRole("status")
      .textContent;
  }

  const basePage = decisionLoopExamples().page;
  // The chip counts the weekly check's unanswered rows: the newest page has none, the stale
  // one keeps the example's one unanswered row.
  resolvers[1]?.({ ...basePage, held_back: [] }); // newest settles first
  await waitFor(() => expect(howStatusText()).toBe("Nothing to do now"));

  resolvers[0]?.(basePage); // stale, settles late
  await act(async () => {}); // give the (wrongly) accepted stale update a chance to apply
  expect(howStatusText()).toBe("Nothing to do now");
});

test("with every row open, no rendered text names jev or System 1", async () => {
  const { container } = render(<DecisionLoopPage {...baseProps()} />);
  await waitFor(() => expect(screen.queryByText("Loading Decision…")).toBeNull());
  await openAllRows();
  expect(container.textContent).not.toMatch(/jev/i);
  expect(container.textContent).not.toMatch(/system 1/i);
});

test("the disclosure chevron and the switch stop animating under data-motion=reduce", () => {
  const css = readCss(import.meta.url, "DecisionLoopPage.css");
  expect(css).toMatch(
    /html\[data-motion="reduce"\] \.dloop-disclosure \.ico,\s*html\[data-motion="reduce"\] \.dloop-switch-track,\s*html\[data-motion="reduce"\] \.dloop-switch-track::after\s*\{\s*transition: none;/,
  );
});
