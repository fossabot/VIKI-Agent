import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { settingsFixture } from "../../testShell";
import { SettingsNav } from "./SettingsNav";
import { SettingsSearch, searchSettings } from "./SettingsSearch";

afterEach(cleanup);

const KEYS = settingsFixture().keys;

function renderSearch(query: string): {
  onQuery: ReturnType<typeof vi.fn>;
  onOpenPage: ReturnType<typeof vi.fn>;
} {
  const onQuery = vi.fn();
  const onOpenPage = vi.fn();
  render(<SettingsSearch keys={KEYS} query={query} onQuery={onQuery} onOpenPage={onOpenPage} />);
  return { onQuery, onOpenPage };
}

test("hits are grouped under their page title, in the nav's order, with a way into each page", () => {
  const { onOpenPage } = renderSearch("name");
  const titles = [...document.querySelectorAll(".settings-search-group-title")].map(
    (node) => node.textContent,
  );
  // "Display name" and "Assistant name" are on General; "Default model"'s description says
  // "model", not "name", so Chat contributes nothing.
  expect(titles).toEqual(["General"]);
  const hits = [...document.querySelectorAll(".settings-search-results li")].map((node) =>
    node.getAttribute("data-setting-key"),
  );
  expect(hits).toEqual(["identity.display_name", "identity.assistant_name"]);
  expect(screen.getByRole("status").textContent).toBe("2 settings match.");
  fireEvent.click(screen.getByRole("button", { name: "Open this page" }));
  expect(onOpenPage).toHaveBeenCalledWith("general");
});

test("the fold is case-insensitive and matches descriptions too", () => {
  expect(searchSettings(KEYS, "LANGUAGE")).toEqual(searchSettings(KEYS, "language"));
  // The description carries the word, the label does not.
  expect(searchSettings(KEYS, "reasoning effort")).toEqual([
    { page: "chat", keys: ["chat.default_effort"] },
  ]);
});

test("a query with no hit says so and lists nothing; an empty query says nothing at all", () => {
  renderSearch("zzzz");
  expect(screen.getByRole("status").textContent).toBe("No settings match.");
  expect(document.querySelector(".settings-search-results")).toBeNull();
  cleanup();
  renderSearch("");
  expect(screen.getByRole("status").textContent).toBe("");
  expect(document.querySelector(".settings-search-results")).toBeNull();
});

test("the field is labelled, capped and findable by the settings.search command", () => {
  const { onQuery } = renderSearch("");
  const input = screen.getByLabelText("Search settings") as HTMLInputElement;
  expect(input.maxLength).toBe(256);
  expect(input.getAttribute("data-region")).toBe("settings-search");
  fireEvent.change(input, { target: { value: "theme" } });
  expect(onQuery).toHaveBeenCalledWith("theme");
});

test("the key that has no control of its own is never a search hit", () => {
  // `onboarding.completed` has no label row, so `searchText` falls back to the raw key — which
  // this query would match were the key not excluded outright.
  expect(searchSettings(KEYS, "onboarding")).toEqual([]);
  expect(searchSettings(KEYS, "   ")).toEqual([]);
});

test("page descriptions make settings without document keys discoverable", () => {
  for (const [query, page] of [
    ["connection", "providers"],
    ["vault", "workspaces"],
    ["assistant responds", "persona"],
    ["AGENTS.md", "persona"],
    ["Persona", "persona"],
    ["troubleshoot", "diagnostics"],
  ]) {
    expect(searchSettings(KEYS, query ?? "").some((group) => group.page === page)).toBe(true);
  }
  renderSearch("connection");
  expect(screen.getByRole("button", { name: "Open this page" })).toBeTruthy();
  expect(screen.getByText("Providers", { selector: "h3" })).toBeTruthy();
});

test("opening a page from a result moves focus to that page's tab, never to the document", () => {
  // The results unmount with the clicked button inside them; without this, focus lands on <body>
  // and a keyboard or screen-reader user is dropped at the top of the page with no announcement.
  render(<SettingsNav page="general" onPage={vi.fn()} panelId="panel" />);
  const { onOpenPage } = renderSearch("density");
  fireEvent.click(screen.getByRole("button", { name: "Open this page" }));
  expect(onOpenPage).toHaveBeenCalledWith("appearance");
  expect(document.activeElement).toBe(document.getElementById("settings-tab-appearance"));
  expect(document.activeElement).not.toBe(document.body);
});
