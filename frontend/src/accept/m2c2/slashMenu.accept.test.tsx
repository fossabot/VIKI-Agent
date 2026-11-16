/**
 * Acceptance (frozen): the `/` menu — one list, the vault's skills first. The rewrite rule
 * is the sharp edge: the user's text is changed only when they pick an item, never because a
 * line happens to start with a bullet.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { SlashMenu } from "../../components/Composer/SlashMenu";
import type { Command } from "../../protocol";

afterEach(cleanup);

const command = (name: string, skill: boolean, description = "", argument_hint = ""): Command => ({
  name,
  description,
  argument_hint,
  skill,
});

const COMMANDS: Command[] = [
  command("retro", true, "VIKI's self-improvement loop"),
  command("research", true, "Deep-dive a topic on the web", "<topic>"),
  command("recap", false, "Generate a one-line session recap now"),
  command("code-review", false, "Review the current diff"),
  command("deep-research", false, "Fan-out web searches"),
];

function show(query: string) {
  const onPick = vi.fn();
  const onClose = vi.fn();
  const { container } = render(
    <SlashMenu commands={COMMANDS} query={query} onPick={onPick} onClose={onClose} />,
  );
  const rows = () => [...container.querySelectorAll(".mi")].map((r) => r.textContent ?? "");
  return { container, onPick, onClose, rows };
}

test("the vault's skills come first, each group under its own label", () => {
  const { container, rows } = show("");
  const labels = [...container.querySelectorAll(".lbl")].map((l) => l.textContent);
  expect(labels).toEqual(["Vault skills", "Commands"]);
  expect(rows()[0]).toContain("/research"); // skills, alphabetical
  expect(rows()[1]).toContain("/retro");
  expect(rows()[2]).toContain("/code-review"); // then the rest, alphabetical
});

test("typing filters anywhere in the name and highlights the match", () => {
  const { container, rows } = show("re");
  expect(rows()).toHaveLength(5);
  expect(container.querySelectorAll(".hl")[0]?.textContent).toBe("re");
  const deep = [...container.querySelectorAll(".mi")].find((r) =>
    (r.textContent ?? "").includes("deep-research"),
  ) as HTMLElement;
  expect(deep.querySelector(".hl")?.textContent).toBe("re");
});

test("an argument hint follows the name", () => {
  const { container } = show("research");
  expect(container.querySelector(".mi")?.textContent).toContain("<topic>");
});

test("the first row starts highlighted, and the arrows move and wrap", () => {
  const { container } = show("ret");
  const on = () => container.querySelector(".mi.on")?.textContent ?? "";
  expect(on()).toContain("/retro");
  fireEvent.keyDown(window, { key: "ArrowDown" });
  expect(on()).toContain("/retro"); // one match: it wraps onto itself
  const many = show("re");
  fireEvent.keyDown(window, { key: "ArrowUp" });
  expect(many.container.querySelector(".mi.on")?.textContent).toContain("/recap"); // last row
});

test("Enter and a click both pick, and Escape closes without picking", () => {
  const { onPick } = show("ret");
  fireEvent.keyDown(window, { key: "Enter" });
  expect(onPick).toHaveBeenCalledWith(expect.objectContaining({ name: "retro" }));
  cleanup();
  const clicked = show("rec");
  /* AMENDMENT (2026-09-13). This line was
     `fireEvent.click(screen.getByText("/recap", { exact: false }))`. getByText's default matcher
     reads only an element's DIRECT text-node children, so the moment the matched run is wrapped
     in the `.hl` span the name reads "/" + "ap" to that query and nothing matches. The
     only way to keep this line green was to suppress the highlight whenever
     exactly one row matched — which is the common tail of every typed query, so the
     orange match would almost never have been seen. This query
     technique was incidental. The row is selected by its class now, which is how the
     rest of this file already works, and the assertion itself is unchanged. */
  const row = [...clicked.container.querySelectorAll<HTMLElement>(".mi")].find((r) =>
    (r.textContent ?? "").includes("/recap"),
  ) as HTMLElement;
  fireEvent.click(row);
  expect(clicked.onPick).toHaveBeenCalledWith(expect.objectContaining({ name: "recap" }));
  cleanup();
  const escaped = show("re");
  fireEvent.keyDown(window, { key: "Escape" });
  expect(escaped.onClose).toHaveBeenCalled();
  expect(escaped.onPick).not.toHaveBeenCalled();
});

test("a query that matches nothing shows the empty note, not an empty box", () => {
  const { container, rows } = show("zzz");
  expect(rows()).toHaveLength(0);
  expect(container.querySelector(".menu-note")?.textContent).toBe("No matching commands");
});

test("a command's description is text, whatever the vault's skill file says", () => {
  const { container } = render(
    <SlashMenu
      commands={[command("evil", true, '<img src=x onerror="window.__pwned=1">')]}
      query=""
      onPick={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  expect(container.querySelector("img")).toBeNull();
  expect(container.textContent).toContain('<img src=x onerror="window.__pwned=1">');
});
