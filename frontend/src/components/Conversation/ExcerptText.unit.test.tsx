import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { ExcerptApi, VaultExcerpt } from "../../state/excerptApi";
import { ExcerptText } from "./ExcerptText";

afterEach(cleanup);

const WORKSPACE = "11111111-2222-4333-8444-555555555555";
const HASH12 = "0123456789ab";

function block(body: string, ref = "notes/plan.md", start = 3, end = 5): string {
  return `[vault excerpt] ${ref}, lines ${start}-${end}, sha256 ${HASH12}\n\`\`\`\n${body}\n\`\`\``;
}

function noteWith(digest12: string): VaultExcerpt {
  return {
    part: {
      kind: "vault_excerpt",
      vault_id: WORKSPACE,
      ref: "notes/plan.md",
      start_line: 3,
      end_line: 5,
      sha256: digest12.padEnd(64, "0"),
    },
    text: "",
    block: "",
    block_bytes: 0,
    revision: "r",
    modified_at: "2026-09-12T09:05:00.000Z",
  };
}

const apiReturning = (note: VaultExcerpt) => ({ excerpt: vi.fn(async () => note) });

function show(text: string, api: ExcerptApi, onOpenVault?: (ref: string, start: number) => void) {
  return render(
    <div id="host">
      <ExcerptText text={text} api={api} workspaceId={WORKSPACE} onOpenVault={onOpenVault} />
    </div>,
  );
}

test("one citation shows its ref, line span and digest, with the body in a pre", () => {
  const { container } = show(block("the body"), apiReturning(noteWith(HASH12)));
  const cite = container.querySelector("details.excerpt-cite") as HTMLElement;
  expect(cite.getAttribute("data-ref")).toBe("notes/plan.md");
  expect(cite.querySelector("summary")?.textContent).toBe("Excerpt: notes/plan.md (lines 3–5)");
  expect(cite.querySelector("pre.excerpt-body")?.textContent).toBe("the body");
  expect(cite.textContent).toContain(`sha256 ${HASH12}`);
});

test("text around a citation stays bare text beside it", () => {
  const { container } = show(`before\n${block("b")}\nafter`, apiReturning(noteWith(HASH12)));
  const host = container.querySelector("#host") as HTMLElement;
  expect([...host.childNodes].map((node) => node.nodeName)).toEqual(["#text", "DETAILS", "#text"]);
});

test("a plain turn is exactly one text node, with no wrapper around it", () => {
  const { container } = show("- milk\n- eggs **not bold**", apiReturning(noteWith(HASH12)));
  const host = container.querySelector("#host") as HTMLElement;
  expect(host.childNodes).toHaveLength(1);
  const only = host.firstChild as ChildNode;
  expect(only.nodeType).toBe(Node.TEXT_NODE);
  expect(only.nodeValue).toBe("- milk\n- eggs **not bold**");
});

test("a half-typed block stays one plain text node", () => {
  const typed = `[vault excerpt] a.md, lines 1-2, sha256 ${HASH12}\n\`\`\`\nno close`;
  const { container } = show(typed, apiReturning(noteWith(HASH12)));
  const host = container.querySelector("#host") as HTMLElement;
  expect(host.childNodes).toHaveLength(1);
  expect(host.firstChild?.nodeValue).toBe(typed);
});

test("compare reads the note once per click and says the note is the same", async () => {
  const api = apiReturning(noteWith(HASH12));
  show(block("b"), api);
  expect(screen.getByRole("status").textContent).toBe("");
  fireEvent.click(screen.getByRole("button", { name: "Compare with the current note" }));
  await waitFor(() =>
    expect(screen.getByRole("status").textContent).toBe("Same as the current note"),
  );
  expect(api.excerpt).toHaveBeenCalledTimes(1);
  expect(api.excerpt).toHaveBeenCalledWith(
    WORKSPACE,
    "notes/plan.md",
    3,
    5,
    expect.any(AbortSignal),
  );
});

test("compare says the note changed when the digest differs", async () => {
  const api = apiReturning(noteWith("ffffffffffff"));
  show(block("b"), api);
  fireEvent.click(screen.getByRole("button", { name: "Compare with the current note" }));
  await waitFor(() => expect(screen.getByRole("status").textContent).toBe("The note has changed"));
  expect(api.excerpt).toHaveBeenCalledTimes(1);
});

test("compare says the note cannot be read when the read fails", async () => {
  const api = { excerpt: vi.fn(async () => Promise.reject(new Error("not_found"))) };
  show(block("b"), api);
  fireEvent.click(screen.getByRole("button", { name: "Compare with the current note" }));
  await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Can't read the note"));
  expect(api.excerpt).toHaveBeenCalledTimes(1);
});

test("a second click asks again, one call per click", async () => {
  const api = apiReturning(noteWith(HASH12));
  show(block("b"), api);
  const button = screen.getByRole("button", { name: "Compare with the current note" });
  fireEvent.click(button);
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(button);
  await waitFor(() => expect(api.excerpt).toHaveBeenCalledTimes(2));
});

test("a body holding a script tag and markdown renders as visible text only", () => {
  const body = "<script>alert(1)</script> **bold** [x](https://example.com) <img src=x onerror=y>";
  const { container } = show(block(body), apiReturning(noteWith(HASH12)));
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("strong")).toBeNull();
  expect(container.querySelector("a")).toBeNull();
  const pre = container.querySelector("pre.excerpt-body") as HTMLElement;
  expect(pre.childNodes).toHaveLength(1);
  expect(pre.firstChild?.nodeType).toBe(Node.TEXT_NODE);
  expect(pre.textContent).toBe(body);
});

test("Open note goes through onOpenVault with the ref and first line, and is absent without it", () => {
  const onOpenVault = vi.fn();
  show(block("b"), apiReturning(noteWith(HASH12)), onOpenVault);
  fireEvent.click(screen.getByRole("button", { name: "Open note" }));
  expect(onOpenVault).toHaveBeenCalledWith("notes/plan.md", 3);
  cleanup();
  show(block("b"), apiReturning(noteWith(HASH12)));
  expect(screen.queryByRole("button", { name: "Open note" })).toBeNull();
});
