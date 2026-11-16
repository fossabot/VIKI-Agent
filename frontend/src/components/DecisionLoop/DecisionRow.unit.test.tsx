import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { createRef, useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { Choice, type ChoiceOption, DecisionRow, Switch, useRowOpen } from "./DecisionRow";

afterEach(() => {
  cleanup();
});

function renderRow(open: boolean, onToggle = vi.fn()) {
  render(
    <DecisionRow
      id="proactive"
      name="Proactive VIKI"
      status="Off"
      control={<button type="button">Turn on</button>}
      open={open}
      onToggle={onToggle}
    >
      <p>Body text</p>
    </DecisionRow>,
  );
  return onToggle;
}

// --- DecisionRow ---

test("DecisionRow is a section named by its disclosure button, with the status and control beside it", () => {
  renderRow(false);
  const section = within(screen.getByRole("region", { name: "Proactive VIKI" }));
  expect(section.getByRole("button", { name: "Proactive VIKI" })).toBeTruthy();
  expect(section.getByRole("status").textContent).toBe("Off");
  expect(section.getByRole("button", { name: "Turn on" })).toBeTruthy();
});

test("DecisionRow's disclosure carries aria-expanded and aria-controls, following open", () => {
  renderRow(true);
  const button = screen.getByRole("button", { name: "Proactive VIKI" });
  expect(button.getAttribute("aria-expanded")).toBe("true");
  expect(button.getAttribute("aria-controls")).toBe("proactive-body");
  expect(document.getElementById("proactive-body")?.hidden).toBe(false);
});

test("DecisionRow hides its body when closed but keeps it mounted", () => {
  renderRow(false);
  expect(screen.getByRole("button", { name: "Proactive VIKI" }).getAttribute("aria-expanded")).toBe(
    "false",
  );
  const body = document.getElementById("proactive-body");
  expect(body).not.toBeNull();
  expect(body?.hidden).toBe(true);
  expect(body?.textContent).toContain("Body text");
});

test("DecisionRow calls onToggle when its disclosure is clicked", () => {
  const onToggle = renderRow(false);
  fireEvent.click(screen.getByRole("button", { name: "Proactive VIKI" }));
  expect(onToggle).toHaveBeenCalledTimes(1);
});

// --- Switch ---

function SwitchHarness({ initial, disabled = false }: { initial: boolean; disabled?: boolean }) {
  const [checked, setChecked] = useState(initial);
  return (
    <>
      <span id="switch-label">Example</span>
      <Switch
        checked={checked}
        labelledBy="switch-label"
        onChange={setChecked}
        disabled={disabled}
      />
    </>
  );
}

test("Switch is role=switch, labelled by the given id, and says Off when unchecked", () => {
  render(<SwitchHarness initial={false} />);
  const el = screen.getByRole("switch");
  expect(el.getAttribute("aria-labelledby")).toBe("switch-label");
  expect(el.getAttribute("aria-checked")).toBe("false");
  expect(el.textContent).toBe("Off");
});

test("Switch says On and aria-checked=true once checked", () => {
  render(<SwitchHarness initial={true} />);
  const el = screen.getByRole("switch");
  expect(el.getAttribute("aria-checked")).toBe("true");
  expect(el.textContent).toBe("On");
});

test("Switch calls onChange with the flipped value when clicked", () => {
  render(<SwitchHarness initial={false} />);
  const el = screen.getByRole("switch");
  fireEvent.click(el);
  expect(el.getAttribute("aria-checked")).toBe("true");
  expect(el.textContent).toBe("On");
});

test("Switch marks aria-disabled and refuses the click while disabled", () => {
  render(<SwitchHarness initial={false} disabled />);
  const el = screen.getByRole("switch");
  expect(el.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(el);
  expect(el.getAttribute("aria-checked")).toBe("false");
});

test("Switch carries no aria-disabled attribute when enabled", () => {
  render(<SwitchHarness initial={false} />);
  expect(screen.getByRole("switch").hasAttribute("aria-disabled")).toBe(false);
});

test("Switch hands its button to a ref", () => {
  const ref = createRef<HTMLButtonElement>();
  render(<Switch ref={ref} checked={false} labelledBy="x" onChange={vi.fn()} />);
  expect(ref.current).toBe(screen.getByRole("switch"));
});

// --- Choice ---

test("Choice sets aria-describedby on its fieldset only when given describedBy", () => {
  const { rerender } = render(
    <Choice label="Pick one" options={OPTIONS} value="a" onChange={vi.fn()} describedBy="why" />,
  );
  expect(screen.getByRole("group").getAttribute("aria-describedby")).toBe("why");
  rerender(<Choice label="Pick one" options={OPTIONS} value="a" onChange={vi.fn()} />);
  expect(screen.getByRole("group").hasAttribute("aria-describedby")).toBe(false);
});

const OPTIONS: readonly ChoiceOption[] = [
  { value: "a", label: "Option A", description: "About A" },
  { value: "b", label: "Option B" },
];

function ChoiceHarness({ initial }: { initial: string }) {
  const [value, setValue] = useState(initial);
  return <Choice label="Pick one" options={OPTIONS} value={value} onChange={setValue} />;
}

test("Choice is a fieldset named by its legend, with one radio per option reflecting value", () => {
  render(<ChoiceHarness initial="a" />);
  const group = within(screen.getByRole("group", { name: "Pick one" }));
  const radios = group.getAllByRole("radio") as HTMLInputElement[];
  expect(radios).toHaveLength(2);
  expect(radios[0]?.checked).toBe(true);
  expect(radios[1]?.checked).toBe(false);
});

test("Choice's options share one generated name, distinct from another Choice's", () => {
  render(
    <>
      <ChoiceHarness initial="a" />
      <ChoiceHarness initial="b" />
    </>,
  );
  const radios = screen.getAllByRole("radio") as HTMLInputElement[];
  expect(radios).toHaveLength(4);
  expect(radios[0]?.name).toBe(radios[1]?.name);
  expect(radios[2]?.name).toBe(radios[3]?.name);
  expect(new Set(radios.map((radio) => radio.name)).size).toBe(2);
});

test("Choice calls onChange with the clicked option's value, which then checks it", () => {
  render(<ChoiceHarness initial="a" />);
  const group = within(screen.getByRole("group", { name: "Pick one" }));
  fireEvent.click(group.getByRole("radio", { name: "Option B" }));
  const radios = group.getAllByRole("radio") as HTMLInputElement[];
  expect(radios[0]?.checked).toBe(false);
  expect(radios[1]?.checked).toBe(true);
});

test("Choice links an option's description by aria-describedby; an option without one has none", () => {
  render(<ChoiceHarness initial="a" />);
  const group = within(screen.getByRole("group", { name: "Pick one" }));
  const [withDesc, withoutDesc] = group.getAllByRole("radio") as HTMLInputElement[];
  const descId = withDesc?.getAttribute("aria-describedby");
  expect(descId).toBeTruthy();
  expect(document.getElementById(descId as string)?.textContent).toBe("About A");
  expect(withoutDesc?.hasAttribute("aria-describedby")).toBe(false);
});

// jsdom fires a disabled radio's React `onChange` anyway on a synthetic `fireEvent.click` (it
// does not replay a real browser's native click-suppression for a disabled control), so this
// only pins the DOM state `disabled` sets — never that it blocks the handler. A caller still
// needs its own guard against writing while disabled, same as every row here already has.
test("Choice's disabled prop disables the fieldset and every radio", () => {
  render(<Choice label="Pick one" options={OPTIONS} value="a" onChange={vi.fn()} disabled />);
  const group = screen.getByRole("group", { name: "Pick one" }) as HTMLFieldSetElement;
  expect(group.disabled).toBe(true);
  const radios = screen.getAllByRole("radio") as HTMLInputElement[];
  expect(radios.every((radio) => radio.disabled)).toBe(true);
});

test("Choice carries no disabled attribute on the fieldset or its radios when not given", () => {
  render(<ChoiceHarness initial="a" />);
  const group = screen.getByRole("group", { name: "Pick one" }) as HTMLFieldSetElement;
  expect(group.disabled).toBe(false);
  for (const radio of screen.getAllByRole("radio") as HTMLInputElement[]) {
    expect(radio.disabled).toBe(false);
  }
});

// --- useRowOpen ---

function RowOpenHarness({ open, onToggle }: { open?: boolean; onToggle?: () => void }) {
  const [resolvedOpen, toggle] = useRowOpen(false, open, onToggle);
  return (
    <>
      <span data-testid="open">{String(resolvedOpen)}</span>
      <button type="button" onClick={toggle}>
        Toggle
      </button>
    </>
  );
}

test("useRowOpen falls back to its own state and default when open/onToggle are both absent", () => {
  render(<RowOpenHarness />);
  expect(screen.getByTestId("open").textContent).toBe("false");
  fireEvent.click(screen.getByRole("button", { name: "Toggle" }));
  expect(screen.getByTestId("open").textContent).toBe("true");
});

test("useRowOpen follows the given open value and calls onToggle instead of flipping its own state", () => {
  const onToggle = vi.fn();
  const { rerender } = render(<RowOpenHarness open={false} onToggle={onToggle} />);
  expect(screen.getByTestId("open").textContent).toBe("false");
  fireEvent.click(screen.getByRole("button", { name: "Toggle" }));
  expect(onToggle).toHaveBeenCalledTimes(1);
  // The click above never flipped any internal state (there is none while controlled); the
  // harness only shows "true" here because the caller now passes open=true itself.
  rerender(<RowOpenHarness open={true} onToggle={onToggle} />);
  expect(screen.getByTestId("open").textContent).toBe("true");
});
