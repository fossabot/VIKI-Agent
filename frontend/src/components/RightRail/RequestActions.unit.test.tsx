/**
 * Unit tests for RequestActions: the inline Allow once / Deny controls for one open permission
 * request. Pins the headline (whole, with hidden characters written out) and waiting text, the
 * pending/outcome/error states the module doc describes, the double-submit guard, the pointer
 * hold after the buttons appear or move, where focus goes when a request leaves, and the classes
 * this component must never carry (the roster's own frozen contract and the Cards.tsx tree, same
 * list AgentRuns.tsx's own docstring holds itself to).
 *
 * jsdom lays nothing out and does not move focus the way a browser does, so three things here
 * stand in for the browser and say so where they are used: every box is at 0,0 unless a test
 * says where the browser put it (`placeButtons`), no box overflows unless a test gives it
 * heights, and a button keeps focus when it is disabled, which a browser does not allow.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { PermissionRequestedData } from "../../protocol";
import { type AnswerOutcome, RequestActions, type RequestActionsProps } from "./RequestActions";
import { ANSWER_SETTLE_MS } from "./useAnswerHold";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const at = (...points: number[]): string => String.fromCodePoint(...points);
const RLO = at(0x202e);

const request = (over: Partial<PermissionRequestedData> = {}): PermissionRequestedData => ({
  tool_use_id: "toolu_1",
  kind: "tool",
  tool: "WebFetch",
  summary: "linter-guide.docs.example.com",
  question: "",
  reason: "",
  agent: "",
  plan: "",
  ...over,
});

function buttons() {
  const allow = screen.getByText("Allow once").closest("button") as HTMLButtonElement;
  const deny = screen.getByText("Deny").closest("button") as HTMLButtonElement;
  return { allow, deny };
}

/** Lets the hand-off queued by a request that left (a microtask, after React's commit) run. */
async function settle(): Promise<void> {
  await act(async () => {});
}

test("the headline names the tool and summary, the tool alone when the summary is empty", () => {
  const { container, rerender } = render(
    <RequestActions request={request()} waitingSince={null} nowMs={0} onDecide={vi.fn()} />,
  );
  expect(container.querySelector(".request-actions-head")?.textContent).toBe(
    "Wants to run: WebFetch linter-guide.docs.example.com",
  );
  rerender(
    <RequestActions
      request={request({ tool: "TodoWrite", summary: "" })}
      waitingSince={null}
      nowMs={0}
      onDecide={vi.fn()}
    />,
  );
  expect(container.querySelector(".request-actions-head")?.textContent).toBe(
    "Wants to run: TodoWrite",
  );
});

test("the headline keeps every line of the command in its text", () => {
  // How the lines are painted is the stylesheet's (NeedsYou.unit.test.tsx reads that rule); this
  // pins that the component hands the stylesheet the line breaks to paint.
  const { container } = render(
    <RequestActions
      request={request({ tool: "Bash", summary: "echo ok\nrm -rf ./build" })}
      waitingSince={null}
      nowMs={0}
      onDecide={vi.fn()}
    />,
  );
  expect(container.querySelector(".request-actions-head")?.textContent).toBe(
    "Wants to run: Bash echo ok\nrm -rf ./build",
  );
});

test("the waiting line shows only once waitingSince is known, worded by runElapsed", () => {
  const { container, rerender } = render(
    <RequestActions request={request()} waitingSince={null} nowMs={65_000} onDecide={vi.fn()} />,
  );
  expect(container.querySelector(".request-actions-wait")).toBeNull();
  rerender(
    <RequestActions request={request()} waitingSince={5_000} nowMs={65_000} onDecide={vi.fn()} />,
  );
  expect(container.querySelector(".request-actions-wait")?.textContent).toBe("waiting 1m 0s");
});

test("each button keeps its plain visible text and gains a descriptive aria-label", () => {
  const { container } = render(
    <RequestActions request={request()} waitingSince={null} nowMs={0} onDecide={vi.fn()} />,
  );
  const { allow, deny } = buttons();
  expect(allow.getAttribute("aria-label")).toBe(
    "Allow once: WebFetch linter-guide.docs.example.com",
  );
  expect(deny.getAttribute("aria-label")).toBe("Deny: WebFetch linter-guide.docs.example.com");
  // Nothing hidden and nothing out of view: no line under the command, nothing to describe.
  expect(container.querySelector(".decision-notes")).toBeNull();
  expect(allow.hasAttribute("aria-describedby")).toBe(false);
  expect(deny.hasAttribute("aria-describedby")).toBe(false);
});

describe("hidden characters in the command", () => {
  // Painted raw, the summary below reads as one quoted string; what runs deletes a folder.
  const summary = `echo "safe${RLO}" ; rm -rf ~/notes #`;

  test("are written as marked codes in the headline, with a line that says so", () => {
    const { container } = render(
      <RequestActions
        request={request({ tool: "Bash", summary })}
        waitingSince={null}
        nowMs={0}
        onDecide={vi.fn()}
      />,
    );
    const head = container.querySelector(".request-actions-head");
    expect(head?.textContent).toBe('Wants to run: Bash echo "safe<U+202E>" ; rm -rf ~/notes #');
    expect(
      [...(head?.querySelectorAll("mark.hidden-code") ?? [])].map((m) => m.textContent),
    ).toEqual(["<U+202E>"]);
    expect(container.textContent).not.toContain(RLO);
    expect(container.querySelector(".decision-notes")?.textContent).toBe(
      "Hidden or text-reordering characters are in this text. Each is shown as its code, for example <U+202E>.",
    );
  });

  test("are written as codes in both buttons' names, which the line also describes", () => {
    const { container } = render(
      <RequestActions
        request={request({ tool: "Bash", summary })}
        waitingSince={null}
        nowMs={0}
        onDecide={vi.fn()}
      />,
    );
    const { allow, deny } = buttons();
    expect(allow.getAttribute("aria-label")).toBe(
      'Allow once: Bash echo "safe<U+202E>" ; rm -rf ~/notes #',
    );
    expect(deny.getAttribute("aria-label")).toBe(
      'Deny: Bash echo "safe<U+202E>" ; rm -rf ~/notes #',
    );
    const notes = container.querySelector(".decision-notes");
    expect(notes?.id).toBeTruthy();
    expect(allow.getAttribute("aria-describedby")).toBe(notes?.id);
    expect(deny.getAttribute("aria-describedby")).toBe(notes?.id);
  });

  test("at the very end of the command are kept, not trimmed away", () => {
    // A line separator counts as white space to `trim`, and as an argument to a shell.
    const { container } = render(
      <RequestActions
        request={request({ tool: "Bash", summary: `ls ${at(0x2028)}` })}
        waitingSince={null}
        nowMs={0}
        onDecide={vi.fn()}
      />,
    );
    expect(container.querySelector(".request-actions-head")?.textContent).toBe(
      "Wants to run: Bash ls <U+2028>",
    );
  });
});

describe("a command taller than its box", () => {
  /** Stands for the browser's layout: the headline's box holds `scroll` pixels of text and shows
   * `client` of them. jsdom reports 0 for both everywhere. */
  function sizeHeadline(scroll: number, client: number): void {
    const headline = (el: Element): boolean => el.classList.contains("request-actions-head");
    vi.spyOn(Element.prototype, "scrollHeight", "get").mockImplementation(function (this: Element) {
      return headline(this) ? scroll : 0;
    });
    vi.spyOn(Element.prototype, "clientHeight", "get").mockImplementation(function (this: Element) {
      return headline(this) ? client : 0;
    });
  }

  test("says part of it is out of view, and both buttons are described by that line", () => {
    sizeHeadline(400, 108);
    const { container } = render(
      <RequestActions request={request()} waitingSince={null} nowMs={0} onDecide={vi.fn()} />,
    );
    const notes = container.querySelector(".decision-notes");
    expect(notes?.textContent).toBe("Part of this text is out of view. Scroll to read all of it.");
    const { allow, deny } = buttons();
    expect(allow.getAttribute("aria-describedby")).toBe(notes?.id);
    expect(deny.getAttribute("aria-describedby")).toBe(notes?.id);
  });

  test("says nothing when the box shows all of it", () => {
    sizeHeadline(108, 108);
    const { container } = render(
      <RequestActions request={request()} waitingSince={null} nowMs={0} onDecide={vi.fn()} />,
    );
    expect(container.querySelector(".decision-notes")).toBeNull();
  });
});

test("a click sends exactly one answer and marks both buttons as answered while it is in flight", () => {
  let finish: (outcome: AnswerOutcome) => void = () => {};
  const onDecide = vi.fn(
    () =>
      new Promise<AnswerOutcome>((resolve) => {
        finish = resolve;
      }),
  );
  render(<RequestActions request={request()} waitingSince={null} nowMs={0} onDecide={onDecide} />);
  const { allow, deny } = buttons();
  fireEvent.click(allow);
  fireEvent.click(allow);
  fireEvent.click(deny);
  expect(onDecide).toHaveBeenCalledTimes(1);
  expect(onDecide).toHaveBeenCalledWith(true);
  expect(allow.getAttribute("aria-disabled")).toBe("true");
  expect(deny.getAttribute("aria-disabled")).toBe("true");
  // Never the `disabled` attribute: a browser takes focus off a disabled button and leaves the
  // keyboard at the top of the page. jsdom would keep focus there either way, so the attribute
  // is what this can pin.
  expect(allow.disabled).toBe(false);
  expect(deny.disabled).toBe(false);
  expect(screen.getByRole("status").textContent).toBe("Sending…");
  void finish; // settled in the outcome-specific tests below
});

test("applied leaves nothing to read and keeps the buttons latched shut", async () => {
  const onDecide = vi.fn(async () => "applied" as const);
  render(<RequestActions request={request()} waitingSince={null} nowMs={0} onDecide={onDecide} />);
  const { allow, deny } = buttons();
  await act(async () => {
    fireEvent.click(allow);
  });
  expect(allow.getAttribute("aria-disabled")).toBe("true");
  expect(deny.getAttribute("aria-disabled")).toBe("true");
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  fireEvent.click(deny);
  fireEvent.click(allow);
  expect(onDecide).toHaveBeenCalledTimes(1);
});

test("rejected says the request already ended and stays latched shut", async () => {
  const onDecide = vi.fn(async () => "rejected" as const);
  render(<RequestActions request={request()} waitingSince={null} nowMs={0} onDecide={onDecide} />);
  const { allow, deny } = buttons();
  await act(async () => {
    fireEvent.click(deny);
  });
  expect(deny.getAttribute("aria-disabled")).toBe("true");
  expect(screen.getByRole("status").textContent).toBe(
    "Not recorded: this request has already ended.",
  );
  fireEvent.click(allow);
  expect(onDecide).toHaveBeenCalledTimes(1);
});

test("unknown says delivery is unconfirmed and stays latched shut", async () => {
  const onDecide = vi.fn(async () => "unknown" as const);
  render(<RequestActions request={request()} waitingSince={null} nowMs={0} onDecide={onDecide} />);
  const { allow } = buttons();
  await act(async () => {
    fireEvent.click(allow);
  });
  expect(allow.getAttribute("aria-disabled")).toBe("true");
  expect(screen.getByRole("status").textContent).toBe(
    "Sent; Claude Code has not confirmed it yet.",
  );
  fireEvent.click(allow);
  expect(onDecide).toHaveBeenCalledTimes(1);
});

test("a rejected promise alerts and lets the buttons answer again for a retry", async () => {
  let reject1: (error: unknown) => void = () => {};
  let resolve2: (outcome: AnswerOutcome) => void = () => {};
  const onDecide = vi
    .fn<(allow: boolean) => Promise<AnswerOutcome>>()
    .mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          reject1 = reject;
        }),
    )
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolve2 = resolve;
        }),
    );
  render(<RequestActions request={request()} waitingSince={null} nowMs={0} onDecide={onDecide} />);
  const { allow, deny } = buttons();

  fireEvent.click(allow);
  expect(allow.getAttribute("aria-disabled")).toBe("true");
  await act(async () => {
    reject1(new Error("network"));
  });
  expect(allow.hasAttribute("aria-disabled")).toBe(false);
  expect(deny.hasAttribute("aria-disabled")).toBe(false);
  expect(screen.getByRole("alert").textContent).toBe("Could not send your answer. Try again.");

  fireEvent.click(allow);
  expect(onDecide).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("status").textContent).toBe("Sending…");
  await act(async () => {
    resolve2("applied");
  });
  expect(allow.getAttribute("aria-disabled")).toBe("true");
  expect(screen.queryByRole("alert")).toBeNull();
});

test("never carries the roster's or the card tree's reserved classes", () => {
  const { container } = render(
    <RequestActions request={request()} waitingSince={1_000} nowMs={2_000} onDecide={vi.fn()} />,
  );
  for (const cls of [".card", ".agent", ".s", ".q", ".rt", ".chip"]) {
    expect(container.querySelector(cls)).toBeNull();
  }
});

describe("the pointer hold", () => {
  /** Stands for the browser's layout: where it put the buttons. Returns a setter for `top`. */
  function placeButtons(): (top: number) => void {
    let top = 0;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
      this: HTMLElement,
    ) {
      const here = this.classList.contains("request-actions-buttons") ? top : 0;
      return new DOMRect(0, here, 100, 24);
    });
    return (next) => {
      top = next;
    };
  }

  const held = (container: HTMLElement): boolean =>
    container.querySelector(".request-actions-buttons[data-settling]") !== null;

  async function wait(ms: number): Promise<void> {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  function mount(onDecide: RequestActionsProps["onDecide"], id = "toolu_1") {
    const element = (nowMs: number, toolUseId = id) => (
      <RequestActions
        request={request({ tool_use_id: toolUseId })}
        waitingSince={null}
        nowMs={nowMs}
        onDecide={onDecide}
      />
    );
    const view = render(element(0));
    return {
      ...view,
      again: (nowMs: number, toolUseId = id) => view.rerender(element(nowMs, toolUseId)),
    };
  }

  test("a pointer click on buttons that have just appeared is ignored; half a second later it answers", async () => {
    vi.useFakeTimers();
    const onDecide = vi.fn(() => new Promise<AnswerOutcome>(() => {}));
    const { container } = mount(onDecide);
    const { allow, deny } = buttons();
    expect(held(container)).toBe(true);

    // `detail` is how many presses the pointer has made: 1 for a click, 2 for the second click
    // of a double-click.
    fireEvent.click(allow, { detail: 1 });
    fireEvent.click(deny, { detail: 2 });
    expect(onDecide).not.toHaveBeenCalled();

    await wait(ANSWER_SETTLE_MS - 1);
    expect(held(container)).toBe(true);
    await wait(1);
    expect(held(container)).toBe(false);
    fireEvent.click(allow, { detail: 1 });
    expect(onDecide).toHaveBeenCalledTimes(1);
    expect(onDecide).toHaveBeenCalledWith(true);
  });

  test("a keyboard activation is never held", () => {
    const onDecide = vi.fn(() => new Promise<AnswerOutcome>(() => {}));
    const { container } = mount(onDecide);
    expect(held(container)).toBe(true);
    // Enter or Space on the focused button arrives as a click with `detail` 0.
    fireEvent.click(buttons().deny, { detail: 0 });
    expect(onDecide).toHaveBeenCalledWith(false);
  });

  test("a held press does not take focus, and the click that ends it is held however late it comes", async () => {
    vi.useFakeTimers();
    const onDecide = vi.fn(() => new Promise<AnswerOutcome>(() => {}));
    const { container } = mount(onDecide);
    const { allow } = buttons();

    // fireEvent returns false when the event's default was prevented: no focus change.
    expect(fireEvent.mouseDown(allow, { detail: 1 })).toBe(false);
    await wait(ANSWER_SETTLE_MS);
    expect(held(container)).toBe(false);
    fireEvent.click(allow, { detail: 1 });
    expect(onDecide).not.toHaveBeenCalled();

    // The next press starts after the hold: it and its click go through.
    expect(fireEvent.mouseDown(allow, { detail: 1 })).toBe(true);
    fireEvent.click(allow, { detail: 1 });
    expect(onDecide).toHaveBeenCalledTimes(1);
  });

  test("buttons that a render moved are held again; a render that moved nothing holds nothing", async () => {
    vi.useFakeTimers();
    const place = placeButtons();
    const onDecide = vi.fn(() => new Promise<AnswerOutcome>(() => {}));
    const { container, again } = mount(onDecide);
    await wait(ANSWER_SETTLE_MS);
    expect(held(container)).toBe(false);

    again(1_000);
    expect(held(container)).toBe(false);

    // Something above the buttons went (a row, a footer, a wrapped line): they sit 40px higher.
    place(-40);
    again(2_000);
    expect(held(container)).toBe(true);
    fireEvent.click(buttons().allow, { detail: 2 });
    expect(onDecide).not.toHaveBeenCalled();

    await wait(ANSWER_SETTLE_MS);
    expect(held(container)).toBe(false);
    fireEvent.click(buttons().allow, { detail: 1 });
    expect(onDecide).toHaveBeenCalledTimes(1);
  });

  test("the hold lasts until the buttons have stayed put for the whole wait", async () => {
    vi.useFakeTimers();
    const place = placeButtons();
    const { container, again } = mount(vi.fn());
    await wait(ANSWER_SETTLE_MS);

    place(20);
    again(1_000);
    await wait(ANSWER_SETTLE_MS - 100);
    place(40);
    again(2_000);
    await wait(ANSWER_SETTLE_MS - 1);
    expect(held(container)).toBe(true);
    await wait(1);
    expect(held(container)).toBe(false);
  });

  test("buttons still moving when the wait ends (no render told of it) are held for another", async () => {
    vi.useFakeTimers();
    const place = placeButtons();
    const { container } = mount(vi.fn());
    await wait(ANSWER_SETTLE_MS - 1);
    place(12);
    await wait(1);
    expect(held(container)).toBe(true);
    await wait(ANSWER_SETTLE_MS);
    expect(held(container)).toBe(false);
  });

  test("a move that nothing reported is caught when the press arrives", async () => {
    vi.useFakeTimers();
    const place = placeButtons();
    const onDecide = vi.fn(() => new Promise<AnswerOutcome>(() => {}));
    const { container } = mount(onDecide);
    await wait(ANSWER_SETTLE_MS);
    expect(held(container)).toBe(false);

    place(64);
    fireEvent.click(buttons().allow, { detail: 1 });
    expect(onDecide).not.toHaveBeenCalled();
    expect(held(container)).toBe(true);
  });

  test("a scroll of what the buttons sit in holds them; a scroll of something else does not look", async () => {
    vi.useFakeTimers();
    const place = placeButtons();
    const { container } = mount(vi.fn());
    const elsewhere = document.createElement("div");
    document.body.append(elsewhere);
    await wait(ANSWER_SETTLE_MS);

    place(-30);
    fireEvent.scroll(elsewhere);
    expect(held(container)).toBe(false);
    fireEvent.scroll(container);
    expect(held(container)).toBe(true);
    elsewhere.remove();
  });

  test("a window resize that moved the buttons holds them", async () => {
    vi.useFakeTimers();
    const place = placeButtons();
    const { container } = mount(vi.fn());
    await wait(ANSWER_SETTLE_MS);

    place(18);
    fireEvent(window, new Event("resize"));
    expect(held(container)).toBe(true);
  });

  test("buttons that come to answer another request are held, though they did not move", async () => {
    vi.useFakeTimers();
    const onDecide = vi.fn(() => new Promise<AnswerOutcome>(() => {}));
    const { container, again } = mount(onDecide);
    await wait(ANSWER_SETTLE_MS);
    expect(held(container)).toBe(false);

    again(1_000, "toolu_2");
    expect(held(container)).toBe(true);
    fireEvent.click(buttons().allow, { detail: 1 });
    expect(onDecide).not.toHaveBeenCalled();
  });
});

/** The caller's own shape (RunPeek.tsx): a list of one row's pending requests, each its own
 * RequestActions, under the run's header controls. The list takes focus itself when `listed`,
 * as RunPeek's does, and goes with its last request. "Back" stands for whatever of the row
 * survives once a request is gone (RunStop, Jump, etc.), the detail this file does not own. */
function RequestList({
  ids,
  onDecide,
  listed = true,
}: {
  ids: string[];
  onDecide: RequestActionsProps["onDecide"];
  listed?: boolean;
}) {
  return (
    <div>
      <button type="button">Back</button>
      {ids.length > 0 ? (
        <section aria-label="Waiting for you" tabIndex={listed ? -1 : undefined}>
          {ids.map((id) => (
            <RequestActions
              key={id}
              request={request({ tool_use_id: id, summary: id })}
              waitingSince={null}
              nowMs={0}
              onDecide={onDecide}
            />
          ))}
        </section>
      ) : null}
    </div>
  );
}

/**
 * Where focus goes when the request it was in leaves. Each test starts from a focused button
 * that could still answer: the request was withdrawn, or answered in the chat, while the user
 * was on it. That start is the same in a browser. (After an answer given here, focus is still on
 * the pressed button only because that button is `aria-disabled` and not `disabled`, which the
 * click tests above pin; jsdom would keep focus on a disabled button too, so nothing here could
 * tell the two apart.)
 */
describe("when the focused request leaves", () => {
  test("with another request still listed, focus goes to the list, never to that request's buttons", async () => {
    const { rerender } = render(<RequestList ids={["a", "b"]} onDecide={vi.fn()} />);
    const denyA = screen.getByRole("button", { name: "Deny: WebFetch a" });
    denyA.focus();
    expect(document.activeElement).toBe(denyA);

    rerender(<RequestList ids={["b"]} onDecide={vi.fn()} />);
    await settle();

    expect(document.activeElement).toBe(screen.getByRole("region", { name: "Waiting for you" }));
    // A key on its way down when the request left presses nothing that answers.
    expect(document.activeElement?.closest(".request-actions")).toBeNull();
  });

  test("with a list that cannot take focus, it goes to the nearest control outside every request", async () => {
    const { rerender } = render(
      <RequestList ids={["a", "b", "c"]} onDecide={vi.fn()} listed={false} />,
    );
    const allowB = screen.getByRole("button", { name: "Allow once: WebFetch b" });
    allowB.focus();

    rerender(<RequestList ids={["a", "c"]} onDecide={vi.fn()} listed={false} />);
    await settle();

    // Not "Allow once: WebFetch a", the first control in the list, nor c's.
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Back" }));
  });

  test("with the last request, the list goes too and focus falls back to the run's own surviving controls", async () => {
    const { rerender } = render(<RequestList ids={["a"]} onDecide={vi.fn()} />);
    const allowA = screen.getByRole("button", { name: "Allow once: WebFetch a" });
    allowA.focus();
    expect(document.activeElement).toBe(allowA);

    rerender(<RequestList ids={[]} onDecide={vi.fn()} />);
    await settle();

    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Back" }));
  });

  test("a control that left in the same change is never chosen: the page is read after the change", async () => {
    function Row({ show }: { show: boolean }) {
      return (
        <div>
          <button type="button">Back</button>
          {show ? (
            <div>
              <button type="button">Open this request</button>
              <RequestActions
                request={request()}
                waitingSince={null}
                nowMs={0}
                onDecide={vi.fn()}
              />
            </div>
          ) : null}
        </div>
      );
    }
    const { rerender } = render(<Row show />);
    screen.getByRole("button", { name: "Deny: WebFetch linter-guide.docs.example.com" }).focus();

    rerender(<Row show={false} />);
    await settle();

    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Back" }));
  });

  test("a request that leaves while focus is outside it moves no focus", async () => {
    const { rerender } = render(<RequestList ids={["a", "b"]} onDecide={vi.fn()} />);
    // Nothing is focused: the user was not in the request, so the list must not take focus.
    expect(document.activeElement).toBe(document.body);
    rerender(<RequestList ids={["b"]} onDecide={vi.fn()} />);
    await settle();
    expect(document.activeElement).toBe(document.body);

    screen.getByRole("button", { name: "Back" }).focus();
    const back = document.activeElement;
    rerender(<RequestList ids={[]} onDecide={vi.fn()} />);
    await settle();
    expect(document.activeElement).toBe(back);
  });

  test("focus that something else took as the request left stays where that put it", async () => {
    const { rerender } = render(<RequestList ids={["a", "b"]} onDecide={vi.fn()} />);
    screen.getByRole("button", { name: "Deny: WebFetch a" }).focus();

    rerender(<RequestList ids={["b"]} onDecide={vi.fn()} />);
    const back = screen.getByRole("button", { name: "Back" });
    back.focus();
    await settle();

    expect(document.activeElement).toBe(back);
  });

  test.each(["disabled", "fieldset", "semantic", "hidden", "inert", "display", "visibility"])(
    "the hand-off skips %s candidates and reaches the surviving header control",
    async (unavailable) => {
      function Controls({ show }: { show: boolean }) {
        return (
          <div>
            <fieldset
              disabled={unavailable === "fieldset"}
              aria-disabled={unavailable === "semantic" ? true : undefined}
              hidden={unavailable === "hidden"}
              inert={unavailable === "inert"}
              style={{
                display: unavailable === "display" ? "none" : undefined,
                visibility: unavailable === "visibility" ? "hidden" : undefined,
              }}
            >
              <button
                type="button"
                data-testid="unavailable-control"
                disabled={unavailable === "disabled"}
              >
                Unavailable
              </button>
            </fieldset>
            <button type="button">Back</button>
            {show ? (
              <RequestActions
                request={request()}
                waitingSince={null}
                nowMs={0}
                onDecide={vi.fn()}
              />
            ) : null}
          </div>
        );
      }
      const { rerender } = render(<Controls show />);
      const unavailableFocus = vi.spyOn(screen.getByTestId("unavailable-control"), "focus");
      const deny = screen.getByRole("button", {
        name: "Deny: WebFetch linter-guide.docs.example.com",
      });
      deny.focus();
      expect(document.activeElement).toBe(deny);

      rerender(<Controls show={false} />);
      await settle();

      expect(unavailableFocus).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Back" }));
    },
  );

  test("the hand-off continues when a candidate's focus call does not move focus", async () => {
    function Controls({ show }: { show: boolean }) {
      return (
        <div>
          <div tabIndex={-1} data-testid="ignored-focus" />
          <button type="button">Back</button>
          {show ? (
            <RequestActions request={request()} waitingSince={null} nowMs={0} onDecide={vi.fn()} />
          ) : null}
        </div>
      );
    }
    const { rerender } = render(<Controls show />);
    const ignoredFocus = vi
      .spyOn(screen.getByTestId("ignored-focus"), "focus")
      .mockImplementation(() => {});
    const deny = screen.getByRole("button", {
      name: "Deny: WebFetch linter-guide.docs.example.com",
    });
    deny.focus();
    expect(document.activeElement).toBe(deny);

    rerender(<Controls show={false} />);
    await settle();

    expect(ignoredFocus).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Back" }));
  });
});
