/**
 * DecisionText: the marked codes, the two lines under a request's text, and the measure that
 * decides the second. jsdom lays nothing out (every height is 0), so the measure tests give the
 * box the heights a browser would report and say so.
 */
import { act, cleanup, render } from "@testing-library/react";
import { type ReactElement, useRef } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { DecisionNotes, DecisionText, useOverflowing } from "./DecisionText";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const at = (...points: number[]): string => String.fromCodePoint(...points);
const RLO = at(0x202e);

describe("DecisionText", () => {
  test("text with nothing hidden is one text node, exactly as given", () => {
    const { container } = render(
      <p>
        <DecisionText text={"git status\n  --short <b>"} />
      </p>,
    );
    const paragraph = container.querySelector("p");
    expect(paragraph?.childNodes).toHaveLength(1);
    expect(paragraph?.firstChild?.nodeType).toBe(Node.TEXT_NODE);
    expect(paragraph?.textContent).toBe("git status\n  --short <b>");
    expect(container.querySelector("b")).toBeNull();
  });

  test("each hidden character is written as its code inside a mark, in the order it was sent", () => {
    const { container } = render(
      <p>
        <DecisionText text={`a${RLO}b${at(0x200b)}c`} />
      </p>,
    );
    expect(container.querySelector("p")?.textContent).toBe("a<U+202E>b<U+200B>c");
    expect(
      [...container.querySelectorAll("mark.hidden-code")].map((mark) => mark.textContent),
    ).toEqual(["<U+202E>", "<U+200B>"]);
    expect(container.textContent).not.toContain(RLO);
  });

  test("the same letters typed into the text are not marked", () => {
    const { container } = render(
      <p>
        <DecisionText text="cat <U+202E> notes.md" />
      </p>,
    );
    expect(container.querySelector("mark")).toBeNull();
    expect(container.querySelector("p")?.textContent).toBe("cat <U+202E> notes.md");
  });
});

describe("DecisionNotes", () => {
  test("renders nothing when neither line applies", () => {
    const { container } = render(<DecisionNotes id="n" hidden={false} overflowing={false} />);
    expect(container.firstChild).toBeNull();
  });

  test("says what was done about hidden characters, and that text is out of view", () => {
    const { container, rerender } = render(<DecisionNotes id="n" hidden overflowing={false} />);
    const lines = (): (string | null)[] =>
      [...container.querySelectorAll("#n.decision-notes p")].map((line) => line.textContent);
    expect(lines()).toEqual([
      "Hidden or text-reordering characters are in this text. Each is shown as its code, for example <U+202E>.",
    ]);
    rerender(<DecisionNotes id="n" hidden={false} overflowing />);
    expect(lines()).toEqual(["Part of this text is out of view. Scroll to read all of it."]);
    rerender(<DecisionNotes id="n" hidden overflowing />);
    expect(lines()).toHaveLength(2);
  });
});

describe("useOverflowing", () => {
  function Box({ children }: { children: string }): ReactElement {
    const ref = useRef<HTMLDivElement>(null);
    const overflowing = useOverflowing(ref);
    return (
      <div ref={ref} data-overflowing={overflowing ? "yes" : "no"}>
        {children}
      </div>
    );
  }

  /** Stands for the browser's layout: the box shows 100px and holds ten for each character. */
  function measureByLength(): void {
    vi.spyOn(Element.prototype, "clientHeight", "get").mockReturnValue(100);
    vi.spyOn(Element.prototype, "scrollHeight", "get").mockImplementation(function (this: Element) {
      return (this.textContent ?? "").length * 10;
    });
  }

  const state = (container: HTMLElement): string | undefined =>
    container.querySelector<HTMLElement>("[data-overflowing]")?.dataset.overflowing;

  test("is false where nothing is laid out, as in jsdom", () => {
    const { container } = render(<Box>some text</Box>);
    expect(state(container)).toBe("no");
  });

  test("is true once the box holds more than it shows, a rounding pixel aside", () => {
    measureByLength();
    // 100px of text in a 100px box, then in a 99px box, a difference that rounding alone can
    // produce, then in a 98px one.
    const fits = render(<Box>0123456789</Box>);
    expect(state(fits.container)).toBe("no");
    fits.unmount();
    vi.spyOn(Element.prototype, "clientHeight", "get").mockReturnValue(99);
    expect(state(render(<Box>0123456789</Box>).container)).toBe("no");
    cleanup();
    vi.spyOn(Element.prototype, "clientHeight", "get").mockReturnValue(98);
    expect(state(render(<Box>0123456789</Box>).container)).toBe("yes");
  });

  test("follows content that arrives without a render of the box's owner", async () => {
    measureByLength();
    const { container } = render(<Box>short</Box>);
    expect(state(container)).toBe("no");
    // A plan's Markdown is filled in by its own component after the card has rendered.
    await act(async () => {
      container.querySelector("[data-overflowing]")?.append("…and a great deal more text");
      await Promise.resolve();
    });
    expect(state(container)).toBe("yes");
  });
});
