/**
 * Acceptance (frozen): copying a reply and copying a code block.
 *
 * The whole value of this feature is that what lands on the clipboard is what the user meant to
 * take. So these tests are about the TEXT, not the button: a code block's control must copy the
 * code and nothing around it, and a reply's control must copy the reply's own words and not the
 * tool rows, the thinking, or VIKI's name. A test that only checked "writeText was called" would
 * pass on a control that copies the whole document.
 *
 * The clipboard can refuse — it is a permission-gated API and the page may not be focused — so
 * the refusal is part of the contract, not an edge case: the control says so and never throws.
 * An exception here would unmount the reply the user was trying to copy.
 *
 * Its wording ("Copy" / "Copied" / "Copy failed") follows the
 * grammar of the app's other messages.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Reply } from "../../components/Conversation/Conversation";
import { Markdown } from "../../components/Conversation/Markdown";
import type { Block, Turn } from "../../state/view";

afterEach(cleanup);

let written: string[] = [];
let refuse = false;

beforeEach(() => {
  written = [];
  refuse = false;
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: {
      writeText: (text: string) => {
        if (refuse) return Promise.reject(new Error("not allowed"));
        written.push(text);
        return Promise.resolve();
      },
    },
  });
});

function turn(blocks: Block[], status: Turn["status"] = "success"): Turn {
  return {
    id: "t1",
    user: null,
    blocks,
    status,
    usage: null,
    durationMs: 0,
    history: false,
  };
}

const text = (id: string, body: string): Block => ({ kind: "text", id, text: body, done: true });

function tools(id: string, tool: string, head: string, rest: string): Block {
  return {
    kind: "tools",
    id,
    rows: [
      {
        id,
        tool,
        row: { head, rest, tail: "" },
        done: true,
        isError: false,
        summary: "",
        preview: "",
      },
    ],
  };
}

test("the reply's control copies the reply's own words, and nothing else in the row", async () => {
  const blocks: Block[] = [
    text("b1", "Yes, there are 132."),
    tools("b2", "Bash", "ls", "30-knowledge"),
    text("b3", "Shall I go on?"),
  ];
  render(<Reply turn={turn(blocks)} onOpenUrl={vi.fn()} />);
  fireEvent.click(screen.getByLabelText("Copy reply"));
  await vi.waitFor(() => expect(written).toHaveLength(1));
  // Both text blocks, in order, separated by a blank line — and not "VIKI", not the tool row.
  expect(written[0]).toBe("Yes, there are 132.\n\nShall I go on?");
});

test("a reply with no words of its own offers nothing to copy", () => {
  const blocks: Block[] = [tools("b1", "Read", "", "Home.md")];
  render(<Reply turn={turn(blocks, "running")} onOpenUrl={vi.fn()} />);
  expect(screen.queryByLabelText("Copy reply")).toBeNull();
});

test("a fenced block copies the code alone, and inline code is not a block", async () => {
  render(
    <Markdown
      text={"Some preamble.\n\n```js\nconst a = 1;\n```\n\nAnd some `inline` code."}
      onOpenUrl={vi.fn()}
    />,
  );
  // Exactly one control: the fenced block has one, the inline span does not.
  const buttons = screen.getAllByLabelText("Copy code");
  expect(buttons).toHaveLength(1);
  fireEvent.click(buttons[0] as HTMLElement);
  await vi.waitFor(() => expect(written).toHaveLength(1));
  expect(written[0]?.trim()).toBe("const a = 1;");
  expect(written[0]).not.toContain("preamble");
  expect(written[0]).not.toContain("inline");
});

test("a copy says so, and goes quiet again on its own", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  try {
    render(<Reply turn={turn([text("b1", "ok")])} onOpenUrl={vi.fn()} />);
    const button = screen.getByLabelText("Copy reply");
    expect(button.textContent).toBe("Copy");
    fireEvent.click(button);
    await vi.waitFor(() => expect(button.textContent).toBe("Copied"));
    vi.advanceTimersByTime(3_000);
    expect(button.textContent).toBe("Copy");
  } finally {
    vi.useRealTimers();
  }
});

test("a refused clipboard is reported, not thrown", async () => {
  refuse = true;
  render(<Reply turn={turn([text("b1", "ok")])} onOpenUrl={vi.fn()} />);
  const button = screen.getByLabelText("Copy reply");
  fireEvent.click(button);
  // The promise rejects; the reply must still be standing and the user must be told why the
  // paste they are about to attempt will not have their text in it.
  await vi.waitFor(() => expect(button.textContent).toBe("Copy failed"));
  expect(screen.getByText("ok")).toBeTruthy();
});

test("no clipboard at all is the same as a refusal", async () => {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
  render(<Reply turn={turn([text("b1", "ok")])} onOpenUrl={vi.fn()} />);
  const button = screen.getByLabelText("Copy reply");
  fireEvent.click(button);
  await vi.waitFor(() => expect(button.textContent).toBe("Copy failed"));
});
