import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { GrantLine, type GrantLineProps } from "./GrantLine";

afterEach(cleanup);

const LIMIT = {
  key: "decision.limit.drafts",
  label: "Daily limit: drafts",
  value: 5,
  today: "Today: 1 of 5 drafts",
};

function line(props: Partial<GrantLineProps> = {}) {
  return (
    <GrantLine
      label="Prepare drafts"
      hint="Plans wait for approval."
      grant="ask"
      onGrant={() => {}}
      limits={[LIMIT]}
      onLimit={() => {}}
      disabled={false}
      {...props}
    />
  );
}

test("names the fieldset by its label, with three radios and the value checked", () => {
  render(line());
  const group = screen.getByRole("group", { name: "Prepare drafts" });
  expect(group).toBeTruthy();
  const radios = screen.getAllByRole("radio") as HTMLInputElement[];
  expect(radios.map((r) => r.checked)).toEqual([false, true, false]);
  expect(screen.getByRole("radio", { name: "Allowed" })).toBeTruthy();
});

test("describedBy lists the hint and each note", () => {
  render(line({ notes: ["First note", "Second note"] }));
  const group = screen.getByRole("group", { name: "Prepare drafts" });
  expect(group.getAttribute("aria-describedby")?.split(" ").length).toBe(3);
  expect(group.getAttribute("aria-describedby")).toContain(
    screen.getByText("Plans wait for approval.").id,
  );
  expect(group.getAttribute("aria-describedby")).toContain(screen.getByText("First note").id);
  expect(group.getAttribute("aria-describedby")).toContain(screen.getByText("Second note").id);
});

test("shows no note, More or nested block unless given", () => {
  const { container } = render(line());
  expect(container.querySelector(".dloop-grant-note")).toBeNull();
  expect(container.querySelector(".dloop-grant-nested")).toBeNull();
  expect(container.querySelector("details")).toBeNull();
});

test("shows nested children and More about this when given", () => {
  const { container } = render(line({ children: <p>Nested</p>, more: <p>Long text</p> }));
  expect(container.querySelector(".dloop-grant-nested")?.textContent).toBe("Nested");
  expect(container.querySelector("details summary")?.textContent).toBe("More about this");
  expect(screen.getByText("Long text")).toBeTruthy();
});

test("shows the limit label and today text verbatim", () => {
  render(line());
  expect(screen.getByLabelText("Daily limit: drafts")).toBeTruthy();
  expect(screen.getByText("Today: 1 of 5 drafts")).toBeTruthy();
});

test("passes the grant value and a numeric limit up", () => {
  const onGrant = vi.fn();
  const onLimit = vi.fn();
  render(line({ onGrant, onLimit }));
  fireEvent.click(screen.getByRole("radio", { name: "Allowed" }));
  fireEvent.change(screen.getByLabelText("Daily limit: drafts"), { target: { value: "7" } });
  expect(onGrant).toHaveBeenCalledWith("allowed");
  expect(onLimit).toHaveBeenCalledWith("decision.limit.drafts", 7);
});

test("makes no call while disabled", () => {
  const onGrant = vi.fn();
  const onLimit = vi.fn();
  render(line({ onGrant, onLimit, disabled: true }));
  fireEvent.click(screen.getByRole("radio", { name: "Allowed" }));
  fireEvent.change(screen.getByLabelText("Daily limit: drafts"), { target: { value: "7" } });
  expect(onGrant).not.toHaveBeenCalled();
  expect(onLimit).not.toHaveBeenCalled();
  expect((screen.getByLabelText("Daily limit: drafts") as HTMLSelectElement).disabled).toBe(true);
});

test("shows no radio without a grant", () => {
  render(
    <GrantLine
      label="Vault upkeep proposals"
      hint="Hint"
      limits={[LIMIT]}
      onLimit={() => {}}
      disabled={false}
    />,
  );
  expect(screen.queryAllByRole("radio")).toHaveLength(0);
  expect(screen.getByText("Vault upkeep proposals")).toBeTruthy();
});
