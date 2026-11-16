import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { MissingRecord } from "../../state/sessionOrganization";
import { MissingSessions } from "./MissingSessions";

afterEach(cleanup);

const PINNED: MissingRecord = {
  session_id: "11111111-1111-1111-1111-111111111111",
  title: "Renamed conversation",
  pinned: true,
  archived: false,
  revision: 4,
};
const ARCHIVED: MissingRecord = {
  session_id: "22222222-2222-2222-2222-222222222222",
  title: "",
  pinned: false,
  archived: true,
  revision: 2,
};

test("no records renders nothing", () => {
  const { container } = render(
    <MissingSessions records={[]} onUnpin={vi.fn()} onRemove={vi.fn()} pending={new Set()} />,
  );
  expect(container.innerHTML).toBe("");
});

test("nothing opens a session: no link, no open button", () => {
  const { container } = render(
    <MissingSessions
      records={[PINNED, ARCHIVED]}
      onUnpin={vi.fn()}
      onRemove={vi.fn()}
      pending={new Set()}
    />,
  );
  expect(container.querySelector("a")).toBeNull();
  for (const button of Array.from(container.querySelectorAll("button")))
    expect(button.textContent).not.toMatch(/open/i);
});

test("English copy: title, tags and actions", () => {
  render(
    <MissingSessions
      records={[PINNED, ARCHIVED]}
      onUnpin={vi.fn()}
      onRemove={vi.fn()}
      pending={new Set()}
    />,
  );
  expect(screen.getByText("Conversations whose record is missing")).toBeTruthy();
  expect(screen.getByText("Renamed conversation")).toBeTruthy();
  expect(screen.getByText("Untitled session (22222222)")).toBeTruthy();
  expect(screen.getAllByText("App metadata")).toHaveLength(2);
  expect(screen.getAllByText("Source missing")).toHaveLength(2);
  expect(screen.getByText("Pinned (in this app)")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Unpin" })).toBeTruthy();
  expect(screen.getAllByRole("button", { name: "Remove" })).toHaveLength(2);
});

test("a confirmed profile draft keeps Open and loses Remove", () => {
  render(
    <MissingSessions
      records={[PINNED, ARCHIVED]}
      onUnpin={vi.fn()}
      onRemove={vi.fn()}
      onOpen={vi.fn()}
      pending={new Set()}
      drafts={new Set([PINNED.session_id])}
    />,
  );
  expect(screen.getAllByRole("button", { name: "Remove" })).toHaveLength(1);
  expect(screen.getAllByRole("button", { name: "Open" })).toHaveLength(1);
});

test("no drafts: every record keeps Remove and none offers Open", () => {
  render(
    <MissingSessions
      records={[PINNED, ARCHIVED]}
      onUnpin={vi.fn()}
      onRemove={vi.fn()}
      onOpen={vi.fn()}
      pending={new Set()}
    />,
  );
  expect(screen.getAllByRole("button", { name: "Remove" })).toHaveLength(2);
  expect(screen.queryByRole("button", { name: "Open" })).toBeNull();
});

test("Unpin and Remove call back with the record's own id", () => {
  const onUnpin = vi.fn();
  const onRemove = vi.fn();
  render(
    <MissingSessions
      records={[PINNED]}
      onUnpin={onUnpin}
      onRemove={onRemove}
      pending={new Set()}
    />,
  );
  screen.getByRole("button", { name: "Unpin" }).click();
  screen.getByRole("button", { name: "Remove" }).click();
  expect(onUnpin).toHaveBeenCalledWith(PINNED.session_id);
  expect(onRemove).toHaveBeenCalledWith(PINNED.session_id);
});

test("pending disables that record's actions", () => {
  render(
    <MissingSessions
      records={[PINNED]}
      onUnpin={vi.fn()}
      onRemove={vi.fn()}
      pending={new Set([PINNED.session_id])}
    />,
  );
  expect(screen.getByRole<HTMLButtonElement>("button", { name: "Unpin" }).disabled).toBe(true);
  expect(screen.getByRole<HTMLButtonElement>("button", { name: "Remove" }).disabled).toBe(true);
});

test("a hostile title renders as text and creates no element", () => {
  const hostile: MissingRecord = { ...PINNED, title: "<script>window.pwned = true</script>" };
  const { container } = render(
    <MissingSessions
      records={[hostile]}
      onUnpin={vi.fn()}
      onRemove={vi.fn()}
      pending={new Set()}
    />,
  );
  expect(container.querySelector("script")).toBeNull();
  expect(container.textContent).toContain(hostile.title);
});
