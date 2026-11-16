import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { CheckOutputJson, CheckRunJson, ChecksApi } from "../../clients/code/checksApi";
import { runLine } from "../../clients/code/checkWords";
import { CheckOutput, cleanOutput, POLL_MS } from "./CheckOutput";

const RUN = "00000000-0000-4000-8000-0000000000b1";
const OTHER = "00000000-0000-4000-8000-0000000000b2";
const MAX_CHARS = 2_097_152;

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const run = (over: Partial<CheckRunJson> = {}): CheckRunJson => ({
  run_id: RUN,
  check_id: "00000000-0000-4000-8000-0000000000c1",
  workspace_id: "00000000-0000-4000-8000-0000000000a1",
  worktree_id: null,
  head_oid: null,
  dirty: null,
  outcome: "running",
  exit_code: null,
  duration_ms: null,
  output_bytes: null,
  started_at: "2026-10-04T10:00:00.000Z",
  ended_at: null,
  ...over,
});
const finished = (over: Partial<CheckRunJson> = {}) =>
  run({
    outcome: "passed",
    exit_code: 0,
    duration_ms: 4000,
    ended_at: "2026-10-04T10:00:04.000Z",
    ...over,
  });

const answer = (over: Partial<CheckOutputJson> = {}): CheckOutputJson => ({
  run: run(),
  start: 0,
  next: 0,
  text: "",
  dropped: false,
  done: false,
  ...over,
});

/** A stub whose `output` gives the queued answers in order. */
function stub(...answers: (CheckOutputJson | Error)[]) {
  const output = vi.fn();
  for (const next of answers) {
    output.mockImplementationOnce(async () => {
      if (next instanceof Error) throw next;
      return next;
    });
  }
  return { api: { output } as unknown as ChecksApi, output };
}

const settle = () => act(async () => {});
const tick = (ms = POLL_MS) => act(async () => vi.advanceTimersByTimeAsync(ms));

async function show(api: ChecksApi, onSettled = vi.fn(), runId = RUN) {
  const view = render(<CheckOutput api={api} runId={runId} name="build" onSettled={onSettled} />);
  await settle();
  return { view, onSettled };
}

const pre = () => document.querySelector("pre.check-output");

test("polling continues from next and stops when done", async () => {
  const last = finished();
  const { api, output } = stub(
    answer({ next: 5, text: "one\n" }),
    answer({ start: 5, next: 9, text: "two\n" }),
    answer({ run: last, start: 9, next: 14, text: "end\n", done: true }),
  );
  const { onSettled } = await show(api);
  expect(output).toHaveBeenCalledTimes(1);
  await tick();
  await tick();
  expect(output.mock.calls.map((call) => [call[0], call[1]])).toEqual([
    [RUN, 0],
    [RUN, 5],
    [RUN, 9],
  ]);
  expect(pre()?.textContent).toBe("one\ntwo\nend\n");
  await tick(POLL_MS * 10);
  expect(output).toHaveBeenCalledTimes(3);
  expect(onSettled).toHaveBeenCalledTimes(1);
  expect(onSettled).toHaveBeenCalledWith(last);
});

test("the next poll waits for the interval", async () => {
  const { api, output } = stub(answer({ next: 1, text: "a" }), answer({ next: 2, text: "b" }));
  await show(api);
  await tick(POLL_MS - 1);
  expect(output).toHaveBeenCalledTimes(1);
  await tick(1);
  expect(output).toHaveBeenCalledTimes(2);
});

test("waiting words show before the first text", async () => {
  const { api } = stub(answer());
  await show(api);
  expect(screen.getByRole("status").textContent).toBe("Waiting for output…");
  expect(pre()).toBeNull();
});

test("an empty finished run says there is no output and shows the outcome line", async () => {
  const last = finished();
  const { api } = stub(answer({ run: last, done: true }));
  await show(api);
  expect(screen.getByText("No output.")).toBeTruthy();
  expect(pre()).toBeNull();
  expect(screen.getByRole("status").textContent).toBe(runLine(last));
});

test("the heading names the check and the status waits for output", async () => {
  const { api } = stub(answer());
  await show(api);
  expect(screen.getByRole("heading").textContent).toBe("Output of build");
  expect(screen.getByRole("status").textContent).toBe("Waiting for output…");
});

test("a dropped answer shows the note once, and later answers keep it", async () => {
  const { api } = stub(
    answer({ next: 4, text: "late", dropped: true }),
    answer({ start: 4, next: 5, text: "!" }),
  );
  await show(api);
  expect(screen.getAllByText("Earlier output was dropped.")).toHaveLength(1);
  await tick();
  expect(screen.getAllByText("Earlier output was dropped.")).toHaveLength(1);
  expect(pre()?.textContent).toBe("late!");
});

test("no note while nothing was dropped", async () => {
  const { api } = stub(answer({ next: 1, text: "a" }));
  await show(api);
  expect(screen.queryByText("Earlier output was dropped.")).toBeNull();
});

test("past the cap the oldest lines go and the note shows", async () => {
  const line = `${"x".repeat(99)}\n`;
  const text = line.repeat(Math.ceil((MAX_CHARS + 5000) / line.length));
  const { api } = stub(answer({ next: text.length, text }));
  await show(api);
  const shown = pre()?.textContent ?? "";
  expect(shown.length).toBeLessThanOrEqual(MAX_CHARS);
  expect(shown.length).toBeGreaterThan(MAX_CHARS - 200);
  expect(shown.startsWith(line)).toBe(true);
  expect(screen.getByText("Earlier output was dropped.")).toBeTruthy();
});

test("a failure stops polling and says so", async () => {
  const { api, output } = stub(answer({ next: 1, text: "a" }), new Error("down"));
  const { onSettled } = await show(api);
  await tick();
  expect(screen.getByRole("status").textContent).toBe("The output could not be loaded.");
  await tick(POLL_MS * 10);
  expect(output).toHaveBeenCalledTimes(2);
  expect(onSettled).not.toHaveBeenCalled();
});

test("unmount aborts the call and clears the timer", async () => {
  const { api, output } = stub(answer({ next: 1, text: "a" }));
  const { view } = await show(api);
  const signal = output.mock.calls[0]?.[2] as AbortSignal;
  expect(signal.aborted).toBe(false);
  view.unmount();
  expect(signal.aborted).toBe(true);
  await tick(POLL_MS * 10);
  expect(output).toHaveBeenCalledTimes(1);
});

test("an answer that arrives after unmount changes nothing", async () => {
  let resolve: (value: CheckOutputJson) => void = () => {};
  const output = vi.fn().mockImplementation(
    () =>
      new Promise<CheckOutputJson>((done) => {
        resolve = done;
      }),
  );
  const onSettled = vi.fn();
  const { view } = await show({ output } as unknown as ChecksApi, onSettled);
  view.unmount();
  await act(async () => resolve(answer({ run: finished(), done: true })));
  expect(onSettled).not.toHaveBeenCalled();
  await tick(POLL_MS * 10);
  expect(output).toHaveBeenCalledTimes(1);
});

test("another run starts again from offset 0", async () => {
  const { api, output } = stub(
    answer({ next: 3, text: "old" }),
    answer({ run: run({ run_id: OTHER }), next: 3, text: "new" }),
  );
  const { view, onSettled } = await show(api);
  view.rerender(<CheckOutput api={api} runId={OTHER} name="build" onSettled={onSettled} />);
  await settle();
  expect(output.mock.calls.map((call) => [call[0], call[1]])).toEqual([
    [RUN, 0],
    [OTHER, 0],
  ]);
  expect(pre()?.textContent).toBe("new");
});

test("a carriage return that ends a chunk waits for the next chunk", async () => {
  const { api } = stub(
    answer({ next: 4, text: "10%\r" }),
    answer({ start: 4, next: 8, text: "50%\r" }),
    answer({ run: finished(), start: 8, next: 12, text: "100%", done: true }),
  );
  await show(api);
  expect(pre()?.textContent).toBe("10%");
  await tick();
  expect(pre()?.textContent).toBe("50%");
  await tick();
  expect(pre()?.textContent).toBe("100%");
});

test("a CRLF split between chunks is one line break", async () => {
  const { api } = stub(
    answer({ next: 2, text: "a\r" }),
    answer({ run: finished(), start: 2, next: 4, text: "\nb", done: true }),
  );
  await show(api);
  await tick();
  expect(pre()?.textContent).toBe("a\nb");
});

test("an escape split between chunks is removed whole", async () => {
  const { api } = stub(
    answer({ next: 6, text: "ok\x1b[3" }),
    answer({ start: 6, next: 12, text: "1mred\x1b]0;ti", done: false }),
    answer({ run: finished(), start: 12, next: 16, text: "tle\x07!", done: true }),
  );
  await show(api);
  expect(pre()?.textContent).toBe("ok");
  await tick();
  expect(pre()?.textContent).toBe("okred");
  await tick();
  expect(pre()?.textContent).toBe("okred!");
});

/** What the pane shows for these answers, the last one finishing the run. */
async function showSplit(...texts: string[]) {
  const answers = texts.map((text, at) =>
    answer({ next: at + 1, text, done: at === texts.length - 1 }),
  );
  const { api } = stub(...answers);
  await show(api);
  for (let at = 1; at < texts.length; at++) await tick();
  const shown = pre()?.textContent ?? "";
  cleanup();
  return shown;
}

test("a carriage return before an erase-line that ends a chunk is still held", async () => {
  expect(await showSplit("50%\r\x1b[K", "60%")).toBe("60%");
});

test("a carriage return before a control that ends a chunk is still held", async () => {
  expect(await showSplit("50%\r\x07", "60%")).toBe("60%");
});

test("a carriage return before an erase-line, then a line feed, is one line break", async () => {
  expect(await showSplit("abc\r\x1b[K", "\ndef")).toBe("abc\ndef");
});

const SAMPLES = [
  "build 10%\r\x1b[2K build 50%\r\x1b[2K build 100%\r\n\x1b[32mok\x1b[0m\r\ndone\x07\r\n",
  "a\r\nb\rc\x1b]0;title\x07d\r\n",
  "x\ry\r\x1b[Kz",
];

test("where the polls split the output does not change what shows", async () => {
  for (const sample of SAMPLES) {
    const whole = await showSplit(sample);
    for (let at = 1; at < sample.length; at++) {
      expect(await showSplit(sample.slice(0, at), sample.slice(at)), `split ${at}`).toBe(whole);
    }
  }
  const sample = SAMPLES[2] ?? "";
  const whole = await showSplit(sample);
  for (let a = 1; a < sample.length; a++) {
    for (let b = a + 1; b < sample.length; b++) {
      const parts = [sample.slice(0, a), sample.slice(a, b), sample.slice(b)];
      expect(await showSplit(...parts), `split ${a},${b}`).toBe(whole);
    }
  }
});

test("a finished run flushes an unfinished escape", async () => {
  const { api } = stub(answer({ run: finished(), next: 3, text: "ab\x1b", done: true }));
  await show(api);
  expect(pre()?.textContent).toBe("ab");
});

test("a script line stays text", async () => {
  const text = "<script>window.hacked = 1</script>\n<b>bold</b>\n";
  const { api } = stub(answer({ run: finished(), next: text.length, text, done: true }));
  await show(api);
  expect(document.querySelector("script")).toBeNull();
  expect(document.querySelector("b")).toBeNull();
  expect(pre()?.childNodes).toHaveLength(1);
  expect(pre()?.firstChild?.nodeType).toBe(Node.TEXT_NODE);
  expect(pre()?.textContent).toBe(text);
});

test("cleanOutput removes colour codes", () => {
  expect(cleanOutput("\x1b[1;31mred\x1b[0m plain \x1b[38;5;208mx\x1b[m")).toBe("red plain x");
});

test("cleanOutput removes an OSC title with either terminator", () => {
  expect(cleanOutput("\x1b]0;my title\x07after")).toBe("after");
  expect(cleanOutput("\x1b]2;my title\x1b\\after")).toBe("after");
});

test("cleanOutput removes two-character escapes and control characters but tab and line feed", () => {
  expect(cleanOutput("a\x1b7b\x1b(Bc\x00d\x07e\x08f\tg\nh")).toBe("abcdef\tg\nh");
});

test("cleanOutput lets the last carriage return of a line win", () => {
  expect(cleanOutput("10%\r50%\r100%")).toBe("100%");
  expect(cleanOutput("one\rtwo\nthree\rfour")).toBe("two\nfour");
});

test("cleanOutput turns CRLF into LF", () => {
  expect(cleanOutput("a\r\nb\r\n")).toBe("a\nb\n");
});
