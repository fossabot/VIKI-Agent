import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { type BotHueControls, BotHuesContext, botHue } from "../Bots/botHues";
import type { Hue } from "../Bots/botIconDesign";
import { BotHueField } from "./BotHueField";

afterEach(cleanup);

const ORDER: Hue[] = ["red", "orange", "amber", "green", "teal", "blue", "purple", "pink"];
const LABELS: Record<Hue, string> = {
  blue: "Blue",
  green: "Green",
  orange: "Orange",
  purple: "Purple",
  red: "Red",
  teal: "Teal",
  amber: "Amber",
  pink: "Pink",
};

function renderField(controls: BotHueControls, profileId = "profile-1", name = "Nova") {
  return render(
    <BotHuesContext.Provider value={controls}>
      <BotHueField profileId={profileId} name={name} />
    </BotHuesContext.Provider>,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

it("renders nothing without a set function", () => {
  const { container } = renderField({ hues: {}, icons: {} });
  expect(container.firstChild).toBeNull();
});

it("renders eight named radios in palette order with a marked emblem per hue", () => {
  const { container } = renderField({
    hues: {},
    icons: {},
    set: vi.fn().mockResolvedValue("saved"),
  });
  expect(screen.getAllByRole("radio").map((radio) => radio.getAttribute("value"))).toEqual(ORDER);
  expect(container.querySelectorAll(".bot-hue-avatar")).toHaveLength(8);
  expect(container.querySelectorAll(".bot-hue-selected-check [data-icon=check]")).toHaveLength(1);
  expect(screen.getByRole("radio", { name: "Blue" }).closest("label")?.textContent).toContain(
    "Blue",
  );
  expect(screen.getByRole("status").textContent).toBe("");
});

it("checks the stored hue and shows selected check mark", () => {
  const { container } = renderField({
    hues: { "profile-1": "pink" },
    icons: { "profile-1": "door" },
    set: vi.fn().mockResolvedValue("saved"),
  });
  expect(screen.getByRole<HTMLInputElement>("radio", { name: "Pink" }).checked).toBe(true);
  expect(container.querySelector('[data-bot-hue="pink"] .bot-hue-selected-check')).not.toBeNull();
  expect(
    container.querySelector('[data-bot-hue="pink"] .bot-icon')?.getAttribute("data-icon"),
  ).toBe("door");
  expect(
    container.querySelector('[data-bot-hue="blue"] .bot-icon')?.getAttribute("data-icon"),
  ).toBe("door");
});

it("uses the derived hue before a value is stored", () => {
  renderField({ hues: {}, icons: {}, set: vi.fn().mockResolvedValue("saved") });
  const derived = botHue("profile-1", "Nova", {});
  expect(screen.getByRole<HTMLInputElement>("radio", { name: LABELS[derived] }).checked).toBe(true);
});

it("updates optimistically, stays enabled, preserves focus, and announces a confirmed save", async () => {
  const pending = deferred<"saved" | "superseded">();
  const set = vi.fn().mockReturnValue(pending.promise);
  renderField({ hues: {}, icons: {}, set }, "profile-1", "Nova");
  const derived = botHue("profile-1", "Nova", {});
  const target = ORDER.find((hue) => hue !== derived) as Hue;
  const radio = screen.getByRole<HTMLInputElement>("radio", { name: LABELS[target] });
  act(() => radio.focus());
  fireEvent.click(radio);
  expect(radio.checked).toBe(true);
  expect(radio.disabled).toBe(false);
  expect(document.activeElement).toBe(radio);
  expect(screen.getByRole("status").textContent).toBe("");
  expect(set).toHaveBeenCalledWith("profile-1", { hue: target });
  await act(async () => pending.resolve("saved"));
  expect(screen.getByRole("status").textContent).toBe("Color saved.");
  expect(document.activeElement).toBe(radio);
});

it("keeps the latest optimistic choice when an earlier choice resolves", async () => {
  const first = deferred<"saved" | "superseded">();
  const second = deferred<"saved" | "superseded">();
  const set = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  renderField({ hues: {}, icons: {}, set });
  const picks = ORDER.filter((hue) => hue !== botHue("profile-1", "Nova", {})).slice(0, 2);
  fireEvent.click(screen.getByRole("radio", { name: LABELS[picks[0] as Hue] }));
  fireEvent.click(screen.getByRole("radio", { name: LABELS[picks[1] as Hue] }));
  await act(async () => first.resolve("saved"));
  expect(
    screen.getByRole<HTMLInputElement>("radio", { name: LABELS[picks[1] as Hue] }).checked,
  ).toBe(true);
  expect(screen.getByRole("status").textContent).toBe("");
  await act(async () => second.resolve("saved"));
  expect(screen.getByRole("status").textContent).toBe("Color saved.");
});

it("does not announce a superseded choice", async () => {
  const pending = deferred<"saved" | "superseded">();
  renderField({ hues: {}, icons: {}, set: vi.fn().mockReturnValue(pending.promise) });
  const derived = botHue("profile-1", "Nova", {});
  const target = ORDER.find((hue) => hue !== derived) as Hue;
  fireEvent.click(screen.getByRole("radio", { name: LABELS[target] }));
  await act(async () => pending.resolve("superseded"));
  expect(screen.getByRole("status").textContent).toBe("");
});

it("reverts a failed current choice and lets a later pick save", async () => {
  const set = vi.fn().mockRejectedValueOnce(new Error("nope")).mockResolvedValueOnce("saved");
  renderField({ hues: {}, icons: {}, set });
  const derived = botHue("profile-1", "Nova", {});
  const failed = ORDER.find((hue) => hue !== derived) as Hue;
  const later = ORDER.find((hue) => hue !== derived && hue !== failed) as Hue;
  fireEvent.click(screen.getByRole("radio", { name: LABELS[failed] }));
  await act(async () => Promise.resolve());
  expect(screen.getByRole("alert").textContent).toBe("The color could not be saved. Try again.");
  expect(screen.getByRole<HTMLInputElement>("radio", { name: LABELS[derived] }).checked).toBe(true);
  fireEvent.click(screen.getByRole("radio", { name: LABELS[later] }));
  await act(async () => Promise.resolve());
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByRole("status").textContent).toBe("Color saved.");
  expect(screen.getByRole<HTMLInputElement>("radio", { name: LABELS[later] }).checked).toBe(true);
});

it("does not let a stale rejection revert a later optimistic choice", async () => {
  const first = deferred<"saved" | "superseded">();
  const second = deferred<"saved" | "superseded">();
  const set = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  renderField({ hues: {}, icons: {}, set });
  const picks = ORDER.filter((hue) => hue !== botHue("profile-1", "Nova", {})).slice(0, 2);
  fireEvent.click(screen.getByRole("radio", { name: LABELS[picks[0] as Hue] }));
  fireEvent.click(screen.getByRole("radio", { name: LABELS[picks[1] as Hue] }));
  await act(async () => first.reject(new Error("nope")));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(
    screen.getByRole<HTMLInputElement>("radio", { name: LABELS[picks[1] as Hue] }).checked,
  ).toBe(true);
  await act(async () => second.resolve("saved"));
});
