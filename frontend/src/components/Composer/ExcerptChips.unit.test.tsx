/**
 * The excerpt chips: each part is checked against the note one request at a time (the route
 * admits two at once, so a parallel burst would refuse itself with 429), a changed note blocks
 * Send until the user clicks Refresh or removes it, and nothing refreshes by itself.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { ExcerptApi, VaultExcerpt, VaultExcerptPart } from "../../state/excerptApi";
import { EXCERPTS_PER_TURN, ExcerptChips, withExcerpt } from "./ExcerptChips";

afterEach(cleanup);

const WORKSPACE = "11111111-1111-4111-8111-111111111111";
const NOW = Date.parse("2026-10-02T12:00:00Z");
const EMPTY_TOTAL = "App-added content: — characters in total (limit: 12,000 characters)";

beforeEach(() => {
  vi.spyOn(Date, "now").mockReturnValue(NOW);
});

function part(n: number, sha = "a".repeat(64)): VaultExcerptPart {
  return {
    kind: "vault_excerpt",
    vault_id: WORKSPACE,
    ref: `notes/n${n}.md`,
    start_line: n,
    end_line: n + 2,
    sha256: sha,
  };
}

function answer(of: VaultExcerptPart, bytes = 100, sha = of.sha256, block = "block"): VaultExcerpt {
  return {
    part: { ...of, sha256: sha },
    text: "body",
    block,
    block_bytes: bytes,
    revision: "r",
    modified_at: "2026-10-02T09:00:00Z",
  };
}

interface Call {
  ref: string;
  signal: AbortSignal | undefined;
  settle: (value: VaultExcerpt | Error) => Promise<void>;
}

/** An api whose every request stays open until the test settles it. */
function manualApi() {
  const calls: Call[] = [];
  let open = 0;
  let mostOpen = 0;
  const api: ExcerptApi = {
    excerpt(_id, ref, _start, _end, signal) {
      open += 1;
      mostOpen = Math.max(mostOpen, open);
      return new Promise<VaultExcerpt>((resolve, reject) => {
        calls.push({
          ref,
          signal,
          settle: async (value) => {
            open -= 1;
            await act(async () => {
              if (value instanceof Error) reject(value);
              else resolve(value);
            });
          },
        });
      });
    },
  };
  return { api, calls, mostOpen: () => mostOpen };
}

function mount(
  parts: VaultExcerptPart[],
  api: ExcerptApi,
  handlers: Partial<Parameters<typeof ExcerptChips>[0]> = {},
) {
  const onRemove = vi.fn();
  const onRefresh = vi.fn();
  const view = render(
    <ExcerptChips
      parts={parts}
      api={api}
      workspaceId={WORKSPACE}
      onRemove={onRemove}
      onRefresh={onRefresh}
      {...handlers}
    />,
  );
  return { ...view, onRemove, onRefresh };
}

test("eight chips are checked one at a time, each waiting for the one before it", async () => {
  const { api, calls, mostOpen } = manualApi();
  const parts = Array.from({ length: 8 }, (_, i) => part(i + 1));
  mount(parts, api);

  for (let i = 0; i < 8; i += 1) {
    // Only chip i is in flight; the others have not been asked about yet.
    expect(calls).toHaveLength(i + 1);
    expect(calls[i]?.ref).toBe(`notes/n${i + 1}.md`);
    await calls[i]?.settle(answer(parts[i] as VaultExcerptPart));
  }

  expect(calls).toHaveLength(8);
  expect(mostOpen()).toBe(1);
  expect(screen.getAllByText(/To be sent to Claude/)).toHaveLength(8);
});

test("each check carries its own abort signal", () => {
  const { api, calls } = manualApi();
  mount([part(1)], api);
  expect(calls[0]?.signal).toBeInstanceOf(AbortSignal);
  expect(calls[0]?.signal?.aborted).toBe(false);
});

test("a fresh chip keeps its lines and destination visible and folds diagnostic facts", async () => {
  const { api, calls } = manualApi();
  const p = part(3, "0123456789abcdef".repeat(4));
  const { container } = mount([p], api);
  expect(screen.getByText("Checking…")).toBeTruthy();
  // The size is the rendered block's own characters, the unit EXCERPT_BLOCK_CHARS counts, not the
  // UTF-8 bytes of block_bytes — which for this answer are deliberately a different number.
  await calls[0]?.settle(answer(p, 963, p.sha256, "あ".repeat(321)));
  expect(screen.getByText("notes/n3.md (lines 3–5)")).toBeTruthy();
  const meta = screen.getByText("321 characters · To be sent to Claude");
  expect(meta.closest("details")).toBeNull();
  const summary = screen.getByLabelText("Excerpt details for notes/n3.md, lines 3–5");
  expect(summary.tagName).toBe("SUMMARY");
  const details = container.querySelector("details");
  expect(details?.open).toBe(false);
  expect(screen.getByText(p.sha256).closest("details")).toBe(details);
  expect(screen.getByText("3 hours ago").closest("details")).toBe(details);
  fireEvent.click(summary);
  expect(details?.open).toBe(true);
  expect(calls).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "Refresh" })).toBeNull();
});

test("a ref that looks like markup renders as text, not as an element", async () => {
  const { api, calls } = manualApi();
  const p = { ...part(1), ref: "<img src=x onerror=alert(1)>.md" };
  const { container } = mount([p], api);
  await calls[0]?.settle(answer(p));
  expect(container.querySelector("img")).toBeNull();
  expect(screen.getByText("<img src=x onerror=alert(1)>.md (lines 1–3)")).toBeTruthy();
});

test("the footer shows — until every chip is fresh, then the characters and the budget", async () => {
  const { api, calls } = manualApi();
  const parts = [part(1), part(2)];
  mount(parts, api);
  expect(screen.getByText(EMPTY_TOTAL)).toBeTruthy();
  await calls[0]?.settle(answer(parts[0] as VaultExcerptPart, 100, undefined, "a".repeat(100)));
  expect(screen.getByText(EMPTY_TOTAL)).toBeTruthy();
  await calls[1]?.settle(answer(parts[1] as VaultExcerptPart, 250, undefined, "b".repeat(250)));
  expect(
    screen.getByText("App-added content: 350 characters in total (limit: 12,000 characters)"),
  ).toBeTruthy();
});

test("the line about what Claude Code loads by itself is always shown", () => {
  const { api } = manualApi();
  mount([part(1)], api);
  expect(
    screen.getByText(
      "Content Claude Code loads automatically, such as CLAUDE.md, is not shown here.",
    ),
  ).toBeTruthy();
});

test("a stale chip says so, offers Refresh, and the footer stays —", async () => {
  const { api, calls } = manualApi();
  const p = part(1);
  const onFreshChange = vi.fn();
  mount([p], api, { onFreshChange });
  await calls[0]?.settle(answer(p, 100, "b".repeat(64)));
  expect(
    screen.getByText("The note has changed. Refresh or remove this excerpt before sending."),
  ).toBeTruthy();
  expect(screen.getByRole("button", { name: "Refresh" })).toBeTruthy();
  expect(screen.getByText(EMPTY_TOTAL)).toBeTruthy();
  expect(onFreshChange).toHaveBeenLastCalledWith(false);
});

test("Refresh refreshes only on click, and hands up the part the route returned", async () => {
  const { api, calls } = manualApi();
  const p = part(1);
  const { onRefresh } = mount([p], api);
  const newer = "c".repeat(64);
  await calls[0]?.settle(answer(p, 100, newer));

  // Nothing else is asked for until the user clicks.
  expect(calls).toHaveLength(1);
  expect(onRefresh).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(calls).toHaveLength(2);
  await calls[1]?.settle(answer(p, 100, newer));

  expect(onRefresh).toHaveBeenCalledTimes(1);
  expect(onRefresh).toHaveBeenCalledWith(0, { ...p, sha256: newer });
});

test("a refreshed part is not checked a second time", async () => {
  const { api, calls } = manualApi();
  const p = part(1);
  const newer = "c".repeat(64);
  const view = mount([p], api);
  await calls[0]?.settle(answer(p, 100, newer));
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await calls[1]?.settle(answer(p, 120, newer, "c".repeat(120)));
  view.rerender(
    <ExcerptChips
      parts={[{ ...p, sha256: newer }]}
      api={api}
      workspaceId={WORKSPACE}
      onRemove={vi.fn()}
      onRefresh={vi.fn()}
    />,
  );
  expect(calls).toHaveLength(2);
  expect(
    screen.getByText("App-added content: 120 characters in total (limit: 12,000 characters)"),
  ).toBeTruthy();
});

test.each(["removal", "unmount"])(
  "an explicit refresh is cancelled on target %s and ignores a late answer",
  async (change) => {
    const { api, calls } = manualApi();
    const target = part(1);
    const sibling = part(2);
    const { rerender, unmount, onRefresh } = mount([target, sibling], api);
    await calls[0]?.settle(answer(target, 100, "b".repeat(64)));
    await calls[1]?.settle(answer(sibling));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    const refresh = calls[2];
    expect(refresh?.signal).toBeInstanceOf(AbortSignal);
    expect(refresh?.signal?.aborted).toBe(false);

    if (change === "removal") {
      rerender(
        <ExcerptChips
          parts={[sibling]}
          api={api}
          workspaceId={WORKSPACE}
          onRemove={vi.fn()}
          onRefresh={onRefresh}
        />,
      );
    } else {
      unmount();
    }
    expect(refresh?.signal?.aborted).toBe(true);
    // The fake completes even after cancellation, so the lifetime guard is exercised too.
    await refresh?.settle(answer(target, 100, "c".repeat(64)));
    expect(onRefresh).not.toHaveBeenCalled();
    if (change === "removal") {
      expect(screen.getAllByText(/To be sent to Claude/)).toHaveLength(1);
      expect(screen.queryByText("Could not check this excerpt.")).toBeNull();
    }
  },
);

test("a stale chip's Refresh waits while another chip is still being checked", async () => {
  const { api, calls } = manualApi();
  const parts = [part(1), part(2)];
  mount(parts, api);
  await calls[0]?.settle(answer(parts[0] as VaultExcerptPart, 100, "b".repeat(64)));
  expect((screen.getByRole("button", { name: "Refresh" }) as HTMLButtonElement).disabled).toBe(
    true,
  );
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(calls).toHaveLength(2);
  await calls[1]?.settle(answer(parts[1] as VaultExcerptPart));
  expect((screen.getByRole("button", { name: "Refresh" }) as HTMLButtonElement).disabled).toBe(
    false,
  );
});

test("a note that cannot be read is missing; any other failure is unknown", async () => {
  const { api, calls } = manualApi();
  mount([part(1), part(2)], api);
  await calls[0]?.settle(new Error("not_found"));
  expect(screen.getByText("This note cannot be read. Remove the excerpt.")).toBeTruthy();
  await calls[1]?.settle(new Error("overloaded"));
  expect(screen.getByText("Could not check this excerpt.")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Refresh" })).toBeNull();
});

test("Remove removes the chip it belongs to, by position", async () => {
  const { api, calls } = manualApi();
  const parts = [part(1), part(2)];
  const { onRemove } = mount(parts, api);
  await calls[0]?.settle(answer(parts[0] as VaultExcerptPart));
  await calls[1]?.settle(answer(parts[1] as VaultExcerptPart));
  fireEvent.click(screen.getAllByRole("button", { name: "Remove" })[1] as HTMLElement);
  expect(onRemove).toHaveBeenCalledWith(1);
});

test("a chip removed while it is being checked is abandoned and the next one is asked", async () => {
  const { api, calls } = manualApi();
  const parts = [part(1), part(2)];
  const view = mount(parts, api);
  const first = calls[0];
  view.rerender(
    <ExcerptChips
      parts={[parts[1] as VaultExcerptPart]}
      api={api}
      workspaceId={WORKSPACE}
      onRemove={vi.fn()}
      onRefresh={vi.fn()}
    />,
  );
  expect(first?.signal?.aborted).toBe(true);
  expect(calls).toHaveLength(2);
  expect(calls[1]?.ref).toBe("notes/n2.md");
});

test("a part that is removed and added back is checked again", async () => {
  const { api, calls } = manualApi();
  const p = part(1);
  const view = mount([p], api);
  await calls[0]?.settle(answer(p));
  const again = (parts: VaultExcerptPart[]) => (
    <ExcerptChips
      parts={parts}
      api={api}
      workspaceId={WORKSPACE}
      onRemove={vi.fn()}
      onRefresh={vi.fn()}
    />
  );
  view.rerender(again([]));
  view.rerender(again([p]));
  expect(calls).toHaveLength(2);
  expect(screen.getByText("Checking…")).toBeTruthy();
});

const CAP_LINE = "Each message can include at most 8 excerpts. Remove one to add another.";

test("a ninth attach is refused, and the chips region says what the cap is", () => {
  const full = Array.from({ length: EXCERPTS_PER_TURN }, (_, i) => part(i + 1));
  // A ninth part would make the draft save unparseable (MAX_PARTS in
  // src/clients/conversations/durableApi.ts) and leave a Retry save that could only fail again.
  expect(withExcerpt(full, part(99))).toBe(full);
  const { api } = manualApi();
  mount(full, api);
  expect(screen.getByText(CAP_LINE)).toBeTruthy();
});

test("the cap line is absent while the turn still has room", () => {
  const { api } = manualApi();
  mount(
    Array.from({ length: EXCERPTS_PER_TURN - 1 }, (_, i) => part(i + 1)),
    api,
  );
  expect(screen.queryByText(CAP_LINE)).toBeNull();
});

const BUDGET_LINE =
  "Excerpts exceed the per-message limit of 12,000 characters. Remove an excerpt before sending.";

test("hides diagnostics but keeps stale and budget blockers visible", async () => {
  const { api, calls } = manualApi();
  const fresh = part(1);
  const stale = part(2);
  const onFreshChange = vi.fn();
  const { container } = mount([fresh, stale], api, { onFreshChange });
  for (const message of screen.getAllByText("Checking…")) {
    expect(message.closest("details")).toBeNull();
  }
  await calls[0]?.settle(answer(fresh, 7000, undefined, "x".repeat(7000)));
  await calls[1]?.settle(answer(stale, 7000, "b".repeat(64), "x".repeat(7000)));

  const details = container.querySelector("details");
  expect(details).not.toBeNull();
  expect(details?.open).toBe(false);
  const warning = screen.getByText(
    "The note has changed. Refresh or remove this excerpt before sending.",
  );
  expect(warning.closest("details")).toBeNull();
  expect(screen.getByText(BUDGET_LINE).closest("details")).toBeNull();
  expect(screen.getByText(EMPTY_TOTAL).closest("details")).toBeNull();
  expect(screen.getByRole("button", { name: "Refresh" }).closest("details")).toBeNull();
  expect(onFreshChange).toHaveBeenLastCalledWith(false);

  fireEvent.click(screen.getByLabelText("Excerpt details for notes/n1.md, lines 1–3"));
  expect(details?.open).toBe(true);
  expect(calls).toHaveLength(2);
  expect(onFreshChange).toHaveBeenLastCalledWith(false);
});

test("chips whose blocks pass the turn's character budget hold Send and say to remove one", async () => {
  // Three excerpts of ~5,000 characters are inside every per-part cap and inside the 8-chip
  // cap, so without a guard the send would be accepted, the draft cleared, and the dispatch
  // then refuse excerpts_too_large.
  const { api, calls } = manualApi();
  const parts = [part(1), part(2), part(3)];
  const onFreshChange = vi.fn();
  mount(parts, api, { onFreshChange });
  for (const [index, p] of parts.entries()) {
    await calls[index]?.settle(answer(p, 15_000, undefined, "あ".repeat(5_000)));
  }
  // Every chip is fresh, and the turn is still refused: 15,000 characters over a 12,000 budget.
  expect(screen.getAllByText(/To be sent to Claude/)).toHaveLength(3);
  expect(screen.getByText(BUDGET_LINE)).toBeTruthy();
  expect(
    screen.getByText("App-added content: 15,000 characters in total (limit: 12,000 characters)"),
  ).toBeTruthy();
  expect(onFreshChange).toHaveBeenLastCalledWith(false);
});

test("the budget line is absent while the blocks fit, and Send is free", async () => {
  const { api, calls } = manualApi();
  const parts = [part(1), part(2)];
  const onFreshChange = vi.fn();
  mount(parts, api, { onFreshChange });
  await calls[0]?.settle(answer(parts[0] as VaultExcerptPart, 100, undefined, "あ".repeat(6_000)));
  await calls[1]?.settle(answer(parts[1] as VaultExcerptPart, 100, undefined, "い".repeat(6_000)));
  expect(screen.queryByText(BUDGET_LINE)).toBeNull();
  expect(
    screen.getByText("App-added content: 12,000 characters in total (limit: 12,000 characters)"),
  ).toBeTruthy();
  expect(onFreshChange).toHaveBeenLastCalledWith(true);
});

test("attaching a span already held changes nothing; another span is appended", () => {
  const held = [part(1)];
  const first = held[0] as VaultExcerptPart;
  // The same lines of the same note, as a stale re-read would hand them up: one span, one part,
  // exactly as TurnInput's own span dedupe reads it.
  expect(withExcerpt(held, { ...first, sha256: "b".repeat(64) })).toBe(held);
  expect(withExcerpt(held, part(2))).toEqual([first, part(2)]);
});

test("excerpt actions preserve a Japanese ref without displaying quoted content", async () => {
  const { api, calls } = manualApi();
  const p = { ...part(1), ref: "資料/会議.md" };
  const { container, onRemove, onRefresh } = mount([p], api);
  const privateText = "日本語 <img src=x onerror=alert(1)>";
  await calls[0]?.settle({ ...answer(p), text: privateText, block: privateText });
  expect(calls[0]?.ref).toBe(p.ref);
  expect(screen.getByRole("region", { name: "Excerpts to send" })).toBeTruthy();
  const label = screen.getByText("資料/会議.md (lines 1–3)");
  const remove = screen.getByRole("button", { name: "Remove" });
  expect(remove.getAttribute("aria-describedby")).toBe(label.id);
  expect(screen.queryByText(privateText)).toBeNull();
  expect(container.querySelector("img")).toBeNull();
  expect(onRefresh).not.toHaveBeenCalled();
  fireEvent.click(remove);
  expect(onRemove).toHaveBeenCalledTimes(1);
  expect(onRemove).toHaveBeenCalledWith(0);
});
